'''
This file is part of sigcheck.

sigcheck is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

sigcheck is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with sigcheck.  If not, see <https://www.gnu.org/licenses/>.
'''

import os
import re
import sys
import glob
import json
import pefile
import shutil
import struct
import hashlib
import binascii
import tempfile
import subprocess

from io import BytesIO
from enum import Enum

import volatility.debug as debug
import volatility.utils as utils
import volatility.win32.tasks as tasks
import volatility.win32.modules as modules
import volatility.plugins.filescan as filescan
import volatility.plugins.dumpfiles as dumpfiles
import volatility.plugins.malware.devicetree as dtree

from volatility.renderers import TreeGrid
from volatility.plugins.common import AbstractWindowsCommand

CERTIFICATE_REGEX = re.compile(b'\x30.\x30.\x06.(?P<oid_algorithm>.{5,9})\x05\x00\x04(?P<hash_size>.)')
OPENSSL_REGEX = re.compile(r' *(?P<offset>[0-9]+):d=[0-9]+ +hl=(?P<header_length>[0-9]+) +l= *(?P<length>[0-9]+)')

class ReturnCode(Enum):
    FILEOBJECT_ERROR = (1, 'Unable to read FileObject')
    PE_REBUILT_FAILED = (2, 'Unable to rebuilt PE file')
    PE_CHECKSUM_MISMATCH = (3, 'PE OptionalHeader.CheckSum mismatch')
    PARTIAL_CONTENT_PE_DATA_ERROR = (4, 'Partial file content. Unable to load PE')
    SIGNED_FILE_NOT_VERIFIED = (5, 'Signed file, but not verified')
    CONTENT_SIGNED_NOT_VERIFIED = (6, 'Partial file content. Signed file, but not verified')
    PARTIAL_CONTENT_MAYBE_CATALOG_SIGNED = (7, 'Partial file content. Not signed file (maybe catalog-signed?)')
    PARTIAL_CONTENT_NOT_SIGNED = (8, 'Partial file content. Not signed file')
    AUTHENTICODE_SIGNATURE_MISMATCH_OR_INCORRECT_IMAGEBASE = (9, 'Certificate\'s hash mismatch calculated hash, or incorrect ImageBase during reconstruction')
    AUTHENTICODE_SIGNATURE_MISMATCH = (10, 'Certificate\'s hash mismatch calculated hash')
    CATALOG_SIGNED = (11, 'Verification successful (catalog-signed)')
    MAYBE_CATALOG_SIGNED = (12, 'Not signed file (maybe catalog-signed?)')
    NOT_SIGNED_OR_INCORRECT_IMAGEBASE = (13, 'Not signed file, or incorrect ImageBase during reconstruction')
    NOT_SIGNED = (14, 'Not signed file')
    NOT_PEB = (15, 'Unable to read PEB')
    ALREADY_TERMINATED = (16, 'Already terminated')
    PARTIAL_CERTIFICATE = (17, 'Embedded certificate incomplete')
    PARTIAL_CONTENT_VERIFIED = (18, 'Partial file content. Unable to compare file hash and signature hash')

    def __int__(self):
        return self.value[0]

    def __str__(self):
        return self.value[1]

class SigCheck(AbstractWindowsCommand):
    '''
    Attempts to validate Authenticode-signed processes, either with embedded signature or catalog-signed

    Options:
        --catalog [dir]: directory containing catalog files (.cat), default to '$PWD/catroot/$VOL_PROFILE'
    '''

    def __init__(self, config, *args, **kwargs):
        '''
        Plugin creation, it creates temorary files to later working on them
        '''

        AbstractWindowsCommand.__init__(self, config, *args, **kwargs)
        self.__plugin_name, _ = os.path.splitext(os.path.basename(__file__))
        default_catlog_dir = os.path.join('catroot', self._config.get_value("PROFILE"))
        self._config.add_option('CATALOG', help='Catalog dir to search signature into, default to \'$PWD/catroot/$VOL_PROFILE\'', action='store', type='string', default=default_catlog_dir)
        self._config.add_option('DLL', help='Also verify DLL modules (.dll)', action='store_true')
        self._config.add_option('SYS', help='Also verify driver modules (.sys)', action='store_true')
        self.addr_space = utils.load_as(self._config)
        self.files = []
        self.frequent_addresses = self.load_frequent_addresses()

        self.check_args()

        _, self.temp_filename = tempfile.mkstemp()
        _, self.file_cert = tempfile.mkstemp()
        _, self.file_signed_data = tempfile.mkstemp()

        # Simple cache
        self.already_analyzed = {}

    def load_frequent_addresses(self):
        try:
            address_file = 'addresses.json'
            profile = self._config.get_value("PROFILE")
            data = self.load_json(address_file)

            for key in data.keys():
                if profile == key:
                    return data[key]

            self.__debug_message('warning', '\'{0}\': Profile not found, trying to reconstruct ImageSectionObjects with \'{1}\''.format(profile, key))
            return data[key]
        except IOError:
            self.__debug_message('error', 'Unable to load most frequent addresses (\'{0}\' file) '.format(address_file))

    def load_json(self, path):
        with open(path, 'r') as f:
            return json.load(f)

    def check_args(self):
        if not os.path.isdir(self._config.catalog):
            self.__debug_message('error', '\'{0}\': Not a directory (--catalog or -h)'.format(os.path.realpath(self._config.catalog)))

        if self._config.DLL and self._config.SYS:
            self.__debug_message('error', 'Incompatible options: either exe (default), or exe with dll (--dll), or sys (--sys)')

    def calculate(self):
        '''
        Main plugin function

        @return a tuple of process name, process identifier, and process verification result
        '''

        try:
            modules = []

            if self._config.SYS:
                modules += self.get_sys_modules()
            else:
                for task in tasks.pslist(self.addr_space):
                    modules += self.get_pe_modules(task, dlls=self._config.DLL)

            if modules:
                self.files = self.get_files()

                for module in modules:
                    module_path, module_name, pid = module
                    if module_path in self.already_analyzed:
                        yield module_name, pid, self.already_analyzed[module_path]
                    else:
                        if module_path:
                            is_complete, file_object = self.get_file_object(module_path)
                            # We found a complete FileObject to work on
                            if is_complete:
                                result = self.validate_file(file_object)
                                self.already_analyzed[module_path] = result
                                yield module_name, pid, result
                            # We are restricted to likely find signature in last page
                            else:
                                result = self.validate_partial_file(file_object)
                                self.already_analyzed[module_path] = result
                                yield module_name, pid, result
                        # Sometimes, terminated processes are still listed
                        elif task.ExitTime:
                            yield task.ImageFileName, pid, ReturnCode.ALREADY_TERMINATED
                        else:
                            yield task.ImageFileName, pid, ReturnCode.NOT_PEB
        finally:
            self.clean_workin_dir()

    def clean_workin_dir(self):
        '''
        Deletes temporary files
        '''

        self.delete_file(self.temp_filename)
        self.delete_file(self.file_cert)
        self.delete_file(self.file_signed_data)

    def delete_file(self, path):
        if os.path.exists(path):
            os.remove(path)

    def get_files(self):
        '''
        Uses FileScan plugin to retrieve all FileObjects of memory dump

        @return a list of FileObjects
        '''

        self.__debug_message('info', 'Retrieving all file objects, this may take a while...\n')

        ret = []
        scanner = filescan.FileScan(self._config)
        for fileobj in scanner.calculate():
            offset = fileobj.obj_offset
            filename = str(fileobj.file_name_with_device() or '')
            handles = int(fileobj.get_object_header().HandleCount)
            pointers = int(fileobj.get_object_header().PointerCount)
            ret += [{'offset': offset, 'name': filename, 'handles': handles, 'pointers': pointers}]

        return ret

    def get_pe_modules(self, task, dlls=False):
        '''
        Gets executable full paths and base name

        @param task: _EPROCESS structure
        @param dlls: True if search for exe and also dlls

        @return a list of tuples of modules' full path and base name, in that order
        '''

        ret = []

        if dlls:
            for mod in task.get_load_modules():
                ret += [(str(mod.FullDllName), str(mod.BaseDllName), int(task.UniqueProcessId))]
        else:
            if task.Peb:
                for mod in task.get_load_modules():
                    return [(str(mod.FullDllName), str(mod.BaseDllName), int(task.UniqueProcessId))]

        return ret

    def get_sys_modules(self):
        ret = []

        modlist = list(modules.lsmod(self.addr_space))
        mods = dict((self.addr_space.address_mask(mod.DllBase), mod) for mod in modlist)
        mod_addrs = sorted(mods.keys())

        drivers = dtree.DriverIrp(self._config).calculate()    

        for driver in drivers:
            owning_module = tasks.find_module(mods, mod_addrs, mods.values()[0].obj_vm.address_mask(driver.DriverStart))
            if owning_module:
                ret += [(str(owning_module.FullDllName), str(owning_module.BaseDllName), '0')]
            else:
                ret += [('UNKNOWN', 'UNKNOWN', '0')]

        return ret

    def get_file_object(self, filename):
        '''
        Gets file object corresponding to an executable image

        @param filename: executable full path

        @return a dict representing a FileObject:
                    - name: full path
                    - fobj: memory offset
                    - pad: pages NOT memory resident
                    - present: pages memory resident
                    - type: either ImageSectionObject, DataSectionObject, or SharedCacheMap
                    - ofpath: unique file name to dumps content to, if necessary
        '''

        if filename:
            # Use same notation
            filename = self.normalize_filepath(filename)
            for file in self.files:
                # We consider they are the same file if executable path and file object path match
                if re.match(r'^{0}$'.format(filename), file['name'], flags=re.IGNORECASE):
                    return self.extract_object(file)

        return False, None

    def normalize_filepath(self, filepath):
        '''
        Converts filepath to use uniform notation

        @param filepath

        @return normalized filepath
        '''

        to_replace = {
                        '\\SystemRoot': '\\\\Device\\\\HarddiskVolume[0-9]\\\\Windows',
                        '\\\\\\?\\C:': '\\\\Device\\\\HarddiskVolume[0-9]',
                        'C:': '\\\\Device\\\\HarddiskVolume[0-9]'
                    }

        for key in to_replace.keys():
            path = filepath.split(key)

            if len(path) == 2:
                return to_replace[key] + re.escape(path[1])

    def extract_object(self, file_object):
        '''
        Uses DumpFiles plugin to retrieve all FileObjects of memory dump

        @param file_object: FileObject dict
        @param complete: boolean to force file_object to be full memory resident, default to True

        @return a FileObject
        '''

        self._config.DUMP_DIR = '.'                             # Dummy value
        self._config.PHYSOFFSET =  hex(file_object['offset'])
        dumper = dumpfiles.DumpFiles(self._config)

        for dumpfile in dumper.calculate():
            try:
                # File fully memory resident
                if dumpfile['present'] and not dumpfile['pad']:
                    return True, dumpfile
                elif dumpfile['present']:
                    return False, dumpfile
            # SharedCacheMap has no 'present' attribute
            except KeyError:
                self.__debug_message('warning', 'FileObject \'{0}\': SharedCacheMap not supported'.format(dumpfile['name'].split('\\')[-1]))

        return False, None

    def validate_file(self, file_object):
        '''
        Validates signature thanks to ImageSectionObject and DataSectionObject attibutes of FileObjects

        @param file_object: FileOject dict

        @result string with verification process result
        '''

        # Read actual memory data
        content = self.read_file_memory(file_object)
        file_type = self.get_pe_type(file_object)

        # We need to undo executable relocation 
        if file_object['type'] == 'ImageSectionObject':
            return self.validate_image_section(content, file_type)
        # Data is represented as on-disk, maybe with padding at the end
        elif file_object['type'] == 'DataSectionObject':
            return self.validate_data_section(content)

    def get_pe_type(self, file_object):
        return file_object['name'].split('.')[-1].lower()

    def read_file_memory(self, file_object):
        '''
        Reads all memory resident pages of a FileObject

        @param file_object: FileOject dict

        @returns str buffer with all FileObject content
        '''

        of = BytesIO()

        # memory_model = self.addr_space.profile.metadata.get('memory_model', '32bit')

        for mdata in file_object['present']:
            # DumpFiles official plugin does not handle addresses correctly
            # V.g: 0x20002790a000 instead of 0x2790a000
            mdata[0] &= 0xffffffff

            # mdata[0] = memory offset to read
            # mdata[1] = offset in file reconstruction
            # mdata[2] = amount of bytes to read
            rdata = self.addr_space.base.read(mdata[0], mdata[2])

            of.seek(mdata[1])
            if rdata:
                of.write(rdata)
            else:
                self.__debug_message('warning', 'Unable to read memory for file object \'{0}\' at address {1:#x}'.format(file_object['name'], mdata[0]))

        content = of.getvalue()
        of.close()

        return content

    def validate_image_section(self, content, file_type):
        # TODO: refactor
        content = self.delete_padding(content)
        pe = pefile.PE(data=content, fast_load=True)
        is_32bits = self.is_32bits(content)

        if pe.verify_checksum():
            return self.verify_pe(pe)

        for new_imagebase in self.frequent_addresses[file_type]:
            new_imagebase = int(new_imagebase, 16)

            if is_32bits and new_imagebase > 0xffffffff:
                continue

            try:
                pe = pefile.PE(data=content, fast_load=True)
                pe.relocate_image(new_imagebase)
                new_content = self.set_imagebase(new_imagebase, pe.__data__)
                pe = pefile.PE(data=new_content, fast_load=True)

                if pe.verify_checksum():
                    return self.verify_pe(pe)
            # AttributeError: Some PE files doesn't have relocation table
            # struct.error: Some times pe.get_data_from_qword() fails during relocation
            except (AttributeError, struct.error):
                pass

        return ReturnCode.PE_REBUILT_FAILED

    def verify_pe(self, pe):
        cert = self.extract_cert(pe)
        if cert:
            algorithm, hash_file = self.get_digest_from_signature(cert)
            if algorithm:
                digest = self.calculate_pe_digest(algorithm, pe.__data__)
                if hash_file == digest:
                    return self.verify_signature(cert)
                else:
                    return ReturnCode.AUTHENTICODE_SIGNATURE_MISMATCH
            else:
                return ReturnCode.PARTIAL_CERTIFICATE
        else:
            digest = self.calculate_pe_digest('sha1', pe.__data__)
            if self.is_in_catalog(digest):
                return ReturnCode.CATALOG_SIGNED
            else:
                return ReturnCode.NOT_SIGNED

    def get_imagebase(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)

        if self.is_32bits(content):
            return self.unpack_dword(content[nt_headers_addr+0x34:nt_headers_addr+0x34+0x4])
        elif self.is_64bits(content):
            return self.unpack_qword(content[nt_headers_addr+0x30:nt_headers_addr+0x30+0x8])

    def set_imagebase(self, imagebase, content):
        nt_headers_addr = self.get_nt_header_addr(content)

        if self.is_32bits(content):
            return content[:nt_headers_addr+0x34] + self.pack_dword(imagebase) + content[nt_headers_addr+0x34+0x4:]
        elif self.is_64bits(content):
            return content[:nt_headers_addr+0x30] + self.pack_qword(imagebase) + content[nt_headers_addr+0x30+0x8:]

    def is_32bits(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)
            
        magic = content[nt_headers_addr+0x18:nt_headers_addr+0x18+0x2]

        return magic == b'\x0B\x01'

    def is_64bits(self, content):
        nt_headers_addr = self.get_nt_header_addr(content)
            
        magic = content[nt_headers_addr+0x18:nt_headers_addr+0x18+0x2]

        return magic == b'\x0B\x02'

    def get_pe_section(self, pe, section_name):
        for section in pe.sections:
            # Delete null-bytes at end, v.g: '.text\x00\x00'
            if section.Name.rstrip('\x00') == section_name:
                return section

    def get_digest_from_signature(self, signature):
        # $ openssl asn1parse -inform DER -in signature.der
        # https://github.com/torvalds/linux/blob/450313c5d1313e79059031e6185174616f7ea329/lib/oid_registry_data.c

        # OID_signed_data = binascii.unhexlify('2a864886f70d010702') # pkcs7-signedData

        OID_md5 = binascii.unhexlify('2a864886f70d0205')            # md5
        OID_sha1 = binascii.unhexlify('2b0e03021a')                 # sha1
        OID_sha256 = binascii.unhexlify('608648016503040201')       # sha256

        match = CERTIFICATE_REGEX.search(signature)

        if match:
            oid_algorithm = match.group('oid_algorithm')
            hash_size = ord(match.group('hash_size'))
            where = match.end()

            digest = signature[where:where+hash_size]

            if oid_algorithm == OID_md5:
                return 'md5', digest
            elif oid_algorithm == OID_sha1:
                return 'sha1', digest
            elif oid_algorithm == OID_sha256:
                return 'sha256', digest
        else:
            return None, 0x00

    def verify_signature(self, signature):
        SPC_PE_IMAGE_DATA_OBJID = '1.3.6.1.4.1.311.2.1.15'

        self.save_data(self.file_cert, signature)

        # openssl asn1parse -inform DER -in /tmp/tmp0UGO2s
        process = subprocess.Popen(['openssl', 'asn1parse', '-inform', 'DER', '-in', self.file_cert], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = process.communicate()[0].decode("utf-8").split('\n')

        where = [i for i, item in enumerate(output) if SPC_PE_IMAGE_DATA_OBJID in item]

        if where:
            match = OPENSSL_REGEX.search(output[where[0]-2])

            offset = int(match.group('offset'))
            header_length = int(match.group('header_length'))
            length = int(match.group('length'))

            content = signature[offset+header_length:offset+header_length+length]
            self.save_data(self.file_signed_data, content)

            process = subprocess.Popen(['openssl', 'smime', '-verify', '-inform', 'DER', '-in', self.file_cert,
                                        '-binary', '-content', self.file_signed_data, '-purpose', 'any', '-CApath',
                                        '/etc/ssl/certs/', '-out', '/tmp/dummy.txt'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            output = process.communicate()[1].decode("utf-8").split(':')[-1].replace('\n', '')

            # Capitalize first letter
            return output[0].upper() + output[1:]
        else:
            result = [item for item in output if item][-1]
            return result

    def validate_data_section(self, content):
        '''
        Validate signature of a DataSectionObject

        @param content: data retrieved from a DataSectionObject

        @return string with verification process result
        '''

        # Sometimes, there is padding at the end of buffer
        content = self.delete_padding(content)
        pe = pefile.PE(data=content, fast_load=True)

        # Ensure there are no extraction errors
        if pe.verify_checksum():
            # Files can has an embedded signature
            cert = self.extract_cert(pe)
            if cert:
                algorithm, hash_file = self.get_digest_from_signature(cert)
                digest = self.calculate_pe_digest(algorithm, content)
                if hash_file == digest:
                    return self.verify_signature(cert)
                else:
                    return ReturnCode.AUTHENTICODE_SIGNATURE_MISMATCH
            # Or files can has a signature in a separate catalog file
            else:
                # Calculate algorithm hash to later search in catalog files
                digest = self.calculate_pe_digest('sha1', content)
                if self.is_in_catalog(digest):
                    return ReturnCode.CATALOG_SIGNED
                else:
                    return ReturnCode.NOT_SIGNED
        else:
            return ReturnCode.PE_CHECKSUM_MISMATCH

    def calculate_pe_digest(self, algorithm, raw_data):
        '''
        Calculate Authenticode hash given an algorithm

        @param algoritm: md5, sha1, sha256, or other function contained in hashlib
        @param raw_data: PE raw data

        @return calculated hash string
        '''

        # Skip parts omitted by Authenticode hash algorithm
        # http://download.microsoft.com/download/9/c/5/9c5b2167-8017-4bae-9fde-d599bac8184a/authenticode_pe.docx

        nt_headers_addr = self.get_nt_header_addr(raw_data)
        checksum_addr = nt_headers_addr + 0x58

        certificate_table_addr, certificate_virtual_addr, certificate_size = self.get_pe_certificate_attibutes(raw_data)

        # PE header except OptionalHeader.CheckSum and OptionalHeader.SecurityDirectoryEntry, because those fields are modified
        # due to the sign process itself
        data = raw_data[:checksum_addr] + raw_data[checksum_addr+0x04:certificate_table_addr]

        # Skip only embedded signature, there can be data after it
        if (certificate_virtual_addr and certificate_size) != 0x0:
            data += raw_data[certificate_table_addr+0x08:certificate_virtual_addr] + raw_data[certificate_virtual_addr+certificate_size:]
        # Or don't skip anything if signature is not present
        else:
            data += raw_data[certificate_table_addr+0x08:]

        return getattr(hashlib, algorithm)(data).digest()

    def get_nt_header_addr(self, pe_data):
        '''
        Gets NtHeader offset

        @param pe_data: PE raw data

        @return NtHeader offset
        '''
        if pe_data[:2] == b'\x4D\x5A':              # MZ
            nt_headers_addr = self.unpack_dword(pe_data[0x3c:0x3c+0x04])
            nt_headers = pe_data[nt_headers_addr:nt_headers_addr+0x04]
            if nt_headers == b'\x50\x45\x00\x00':   # PE
                return nt_headers_addr

    def get_pe_certificate_attibutes(self, pe_data):
        '''
        Gets SecurityDirectoryEntry offset and its attributes

        @param pe_data: PE raw data

        @return tuple with SecurityDirectoryEntry offset, SecurityDirectoryEntry.VirtualAddress, SecurityDirectoryEntry.Size
        '''
        nt_headers = self.get_nt_header_addr(pe_data)

        if self.is_32bits(pe_data):
            certificate_table_addr = nt_headers + 0x98
        elif self.is_64bits(pe_data):
            certificate_table_addr = nt_headers + 0xa8

        certificate_virtual_addr = self.unpack_dword(pe_data[certificate_table_addr:certificate_table_addr+0x04])
        certificate_size = self.unpack_dword(pe_data[certificate_table_addr+0x04:certificate_table_addr+0x08])

        return certificate_table_addr, certificate_virtual_addr, certificate_size

    def unpack_dword(self, bytes_):
        return struct.unpack('<I', bytes_)[0]

    def pack_dword(self, bytes_):
        return struct.pack('<I', bytes_)

    def unpack_qword(self, bytes_):
        return struct.unpack('<Q', bytes_)[0]

    def pack_qword(self, bytes_):
        return struct.pack('<Q', bytes_)

    def is_in_catalog(self, digest):
        files = self.get_files_by_extension(self._config.catalog, '.cat')

        if files:
            for file in files:
                data = self.read_data(file)
                for match in CERTIFICATE_REGEX.finditer(data):
                    oid_algorithm = match.group('oid_algorithm')
                    hash_size = ord(match.group('hash_size'))
                    where = match.end()

                    hash_digest = data[where:where+hash_size]

                    if digest == hash_digest:
                        return True
        else:
            self.__debug_message('warning', 'Catalog path: \'{0}\': It doesn\'t contain catalog files (.cat)'.format(os.path.realpath(self._config.catalog)))

        return False

    def get_files_by_extension(self, path, extension):
        ret = []

        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    _, ext = os.path.splitext(f)
                    if ext == extension:
                        ret += [os.path.join(root, f)]

        return ret

    def delete_padding(self, content):
        '''
        Deletes padding of SectionObject containing an executable

        @param content: with padding

        @return content without padding
        '''

        real_size = self.calculate_pe_size(content)
        content = content[:real_size]

        return content

    def calculate_pe_size(self, data):
        '''
        Calculate the size of an executable adding size of PE headers, all sections,
        and Authenticode signature

        @param data: executable data

        @return executable size
        '''

        # Assume PE file is well formed
        pe = pefile.PE(data=data, fast_load=True)
        # PE Headers
        size = pe.NT_HEADERS.OPTIONAL_HEADER.SizeOfHeaders
        # All sections
        for section in pe.sections:
            size += section.SizeOfRawData
        # Authenticode signature, if any
        size += pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].Size

        return size

    def extract_cert(self, pe):
        '''
        Extracts Authenticode certificate specified in Security directory entry

        @param pe: pefile.PE object

        @return Authenticode signature
        '''

        security_directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']]

        if self.has_cert(pe):
            return pe.__data__[security_directory.VirtualAddress:security_directory.VirtualAddress+security_directory.Size]

    def has_cert(self, pe):
        security_directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']]

        return (security_directory.Size and security_directory.VirtualAddress) != 0x0

    def validate_partial_file(self, file_object):
        if file_object:
            content = self.read_file_memory(file_object)
            try:
                pe = pefile.PE(data=content, fast_load=True)
                if self.has_cert(pe):
                    if file_object['type'] == 'DataSectionObject':
                        cert = self.extract_cert(pe)
                        if cert:
                            return '{0:s}. Signature verification: {1}'.format(ReturnCode.PARTIAL_CONTENT_VERIFIED, self.verify_signature(cert))
                        else:
                            return ReturnCode.CONTENT_SIGNED_NOT_VERIFIED
                    # SecurityDirectory entry is not mappped into memory in ImageSectionObject
                    elif file_object['type'] == 'ImageSectionObject':
                        return ReturnCode.CONTENT_SIGNED_NOT_VERIFIED
                else:
                    # Microsoft programs in 'C:\Windows' are usually catalog-signed
                    if re.match(r'\Device\HarddiskVolume[0-9]\Windows', file_object['name']):
                        return ReturnCode.PARTIAL_CONTENT_MAYBE_CATALOG_SIGNED

                    return ReturnCode.PARTIAL_CONTENT_NOT_SIGNED
            except pefile.PEFormatError:
                return ReturnCode.PARTIAL_CONTENT_PE_DATA_ERROR
        else:
            return ReturnCode.FILEOBJECT_ERROR

    def read_data(self, filename):
        with open(filename, 'rb') as f:
            return f.read()

    def save_data(self, filename, file_content):
        with open(filename, 'wb') as f:
            f.write(file_content)

    def __debug_message(self, type_, message):
        getattr(debug, type_)('{0}\t: {1}'.format(self.__plugin_name, message))

    def unified_output(self, data):
        return TreeGrid([
                            ('Module', str),
                            ('Pid', int),
                            ('Result', str)
                        ],
                        self.generator(data))

    def generator(self, data):
        for process_name, pid, result in data:
            yield (0, [str(process_name), int(pid), str(result)])

    def render_text(self, outfd, data):
        self.table_header(outfd,
                              [('Module', '25s'),
                               ('Pid', '>6'),
                               ('Result', '120s')]
                         )

        for process_name, pid, result in data:
            self.table_row(outfd,
                           process_name,
                           pid,
                           result
                          )

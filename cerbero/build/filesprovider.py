# cerbero - a multi-platform build system for Open Source software
# Copyright (C) 2012 Andoni Morales Alastruey <ylatuya@gmail.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Library General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Library General Public License for more details.
#
# You should have received a copy of the GNU Library General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import re
import glob
import inspect

from cerbero.config import Platform
from cerbero.build.build import BuildType
from cerbero.utils import shell
from cerbero.utils import messages as m


class FilesProvider(object):
    '''
    List files by categories using class attributes named files_$category and
    platform_files_$category
    '''

    LIBS_CAT = 'libs'
    BINS_CAT = 'bins'
    PY_CAT = 'python'
    DEVEL_CAT = 'devel'
    LANG_CAT = 'lang'
    TYPELIB_CAT = 'typelibs'
    _DLL_REGEX = r'^(lib)?.*(-[0-9]+)?\.dll$'
    _SO_REGEX = r'^lib.*\.so(\.[0-9]+){0,2}$'
    _DYLIB_REGEX = r'^lib.*(\.[0-9]+)?\.dylib$'

    # Extension Glob Legend:
    # bext = binary extension
    # sext = shared library matching regex
    # srext = shared library extension (no regex)
    # sdir = shared library directory
    # mext = module (plugin) extension
    # smext = static module (plugin) extension
    # pext = python module extension (.pyd on Windows)
    EXTENSIONS = {
        # NOTE: 'sext' is modified in _search_libraries for recipes built with MSVC
        Platform.WINDOWS: {'bext': '.exe', 'sext': _DLL_REGEX, 'sdir': 'bin',
            'mext': '.dll', 'smext': '.a', 'pext': '.pyd', 'srext': '.dll'},
        Platform.LINUX: {'bext': '', 'sext': _SO_REGEX, 'sdir': 'lib',
            'mext': '.so', 'smext': '.a', 'pext': '.so', 'srext': '.so'},
        Platform.ANDROID: {'bext': '', 'sext': _SO_REGEX, 'sdir': 'lib',
            'mext': '.so', 'smext': '.a', 'pext': '.so', 'srext': '.so'},
        Platform.DARWIN: {'bext': '', 'sext': _DYLIB_REGEX, 'sdir': 'lib',
            'mext': '.so', 'smext': '.a', 'pext': '.so', 'srext': '.dylib'},
        Platform.IOS: {'bext': '', 'sext': _DYLIB_REGEX, 'sdir': 'lib',
            'mext': '.so', 'smext': '.a', 'pext': '.so', 'srext': '.dylib'}}

    def __init__(self, config):
        self.config = config
        self.platform = config.target_platform
        self.extensions = self.EXTENSIONS[self.platform]
        self.py_prefix = config.py_prefix
        self.categories = self._files_categories()
        self._searchfuncs = {self.LIBS_CAT: self._search_libraries,
                             self.BINS_CAT: self._search_binaries,
                             self.PY_CAT: self._search_pyfiles,
                             self.LANG_CAT: self._search_langfiles,
                             self.TYPELIB_CAT: self._search_typelibfiles,
                             # TODO: Add a search function for plugins instead
                             #       of handling it in the default handler
                             'default': self._search_files}

    def devel_files_list(self):
        '''
        Return the list of development files, which consists in the files and
        directories listed in the 'devel' category and the link libraries .a,
        .la and .so from the 'libs' category
        '''
        devfiles = self.files_list_by_category(self.DEVEL_CAT)
        devfiles.extend(self._search_girfiles())
        devfiles.extend(self._search_devel_libraries())

        return sorted(list(set(devfiles)))

    def dist_files_list(self):
        '''
        Return the list of files that should be included in a distribution
        tarball, which include all files except the development files
        '''

        return self.files_list_by_categories(
            [x for x in self.categories if not x.endswith(self.DEVEL_CAT)])

    def files_list(self):
        '''
        Return the complete list of files
        '''
        files = self.dist_files_list()
        files.extend(self.devel_files_list())
        return sorted(list(set(files)))

    def files_list_by_categories(self, categories):
        '''
        Return the list of files in a list categories
        '''
        files = []
        for cat in categories:
            files.extend(self._list_files_by_category(cat))
        return sorted(list(set(files)))

    def files_list_by_category(self, category):
        '''
        Return the list of files in a given category
        '''
        return self.files_list_by_categories([category])

    def libraries(self):
        '''
        Return a list of the libraries
        '''
        return self.files_list_by_category(self.LIBS_CAT)

    def use_gobject_introspection(self):
        return self.TYPELIB_CAT in self._files_categories()

    def _files_categories(self):
        ''' Get the list of categories available '''
        categories = []
        for name, value in inspect.getmembers(self):
            if (isinstance(value, list) or isinstance(value, dict)):
                if name.startswith('files_'):
                    categories.append(name.split('files_')[1])
                if name.startswith('platform_files_'):
                    categories.append(name.split('platform_files_')[1])
        return sorted(list(set(categories)))

    def _get_category_files_list(self, category):
        '''
        Get the raw list of files in a category, without pattern match nor
        extensions replacement, which should be done in the search function
        '''
        files = []
        for attr in dir(self):
            if attr.startswith('files_') and attr.endswith('_' + category):
                files.extend(getattr(self, attr))
            if attr.startswith('platform_files_') and \
                    attr.endswith('_' + category):
                files.extend(getattr(self, attr).get(self.platform, []))
        return files

    def _list_files_by_category(self, category):
        search_category = category
        if category.startswith(self.LIBS_CAT + '_'):
            search_category = self.LIBS_CAT
        search = self._searchfuncs.get(search_category,
                                       self._searchfuncs['default'])
        return search(self._get_category_files_list(category))

    def _search_files(self, files):
        '''
        Search files in the prefix, doing the extension replacements and
        listing directories
        '''
        # replace extensions
        files_expanded = [f % self.extensions for f in files]
        fs = []
        # FIXME FIXME FIXME: TERRIBLE HACK. FIX WITH A PROPER PLUGIN SEARCHFUNC
        # Needs to be aware of the toolchain used to know what the output
        # DLL filename of the plugins will be
        for f in files_expanded:
            if f.endswith('.dll') and 'giognutls' not in f and \
               'gstnice' not in f and 'gstlibav' not in f and \
               'gstrtspclientsink' not in f:
                # plugins and modules that are built with meson
                f = f.replace('libgst', 'gst')
                fs.append(f)
                # PDBs are only available when buildtype is not release
                # FIXME: This should be done as part of devel-plugin search
                if not self.config.variants.nodebug:
                    # HACK: Add pdb file with the DLL. This should go into the
                    # devel files list and the devel package.
                    fs.append(f.replace('.dll', '.pdb'))
            else:
                fs.append(f)
        # fill directories
        dirs = [x for x in fs if
                os.path.isdir(os.path.join(self.config.prefix, x))]
        for directory in dirs:
            fs.remove(directory)
            fs.extend(self._ls_dir(os.path.join(self.config.prefix,
                                                directory)))
        # fill paths with pattern expansion *
        paths = [x for x in fs if '*' in x]
        if len(paths) != 0:
            for path in paths:
                fs.remove(path)
            fs.extend(shell.ls_files(paths, self.config.prefix))
        return fs

    def _search_binaries(self, files):
        '''
        Search binaries in the prefix. This function doesn't do any real serach
        like the others, it only preprend the bin/ path and add the binary
        extension to the given list of files
        '''
        binaries = []
        for f in files:
            self.extensions['file'] = f
            binaries.append('bin/%(file)s%(bext)s' % self.extensions)
        return binaries

    def _search_libraries(self, files):
        '''
        Search libraries in the prefix. Unfortunately the filename might vary
        depending on the platform and we need to match the library name and
        it's extension. There is a corner case on windows where a libray might
        be named foo.dll, foo-1.dll, libfoo.dll, or libfoo-1.dll
        '''
        dlls = []
        libdir = self.extensions['sdir']
        libregex = self.extensions['sext']
        libext = self.extensions['srext']

        libsmatch = []
        notfound = []
        for f in files:
            # Find all files that vaguely match this library
            fpath = os.path.join(libdir, '*{0}*{1}*'.format(f[3:], libext))
            found = glob.glob(os.path.join(self.config.prefix, fpath))
            # Find which of those actually match via an exact regex
            # Ideally Python should provide a function for regex file 'globbing'
            for each in found:
                fname = os.path.basename(each)
                if re.match(libregex, fname):
                    libsmatch.append(os.path.join(libdir, fname))
                    break
            else:
                notfound.append(f)

        if notfound:
            msg = "Some libraries weren't found while searching!"
            for each in notfound:
                msg += '\n' + each
            m.warning(msg)
        return libsmatch

    def _pyfile_get_name(self, f):
        if os.path.exists(os.path.join(self.config.prefix, f)):
            return f
        else:
            pydir = os.path.basename(os.path.normpath(self.config.py_prefix))
            pyversioname = re.sub("python|\.", '', pydir)
            cpythonname = "cpython-" + pyversioname

            splitedext = os.path.splitext(f)
            for ex in ['', 'm']:
                f = splitedext[0] + '.' + cpythonname + ex + splitedext[1]
                if os.path.exists(os.path.join(self.config.prefix, f)):
                    return f
        return None

    def _pyfile_get_cached(self, f):
        pyfiles = []
        pycachedir = os.path.join(os.path.dirname(f), "__pycache__")
        for e in ['o', 'c']:
            fe = self._pyfile_get_name(f + e)
            if fe:
                pyfiles.append(fe)
            else:
                cached = os.path.join(pycachedir, os.path.basename(f))
                fe = self._pyfile_get_name(os.path.join(self.config.prefix, cached))
                if fe:
                    pyfiles.append(fe)

        return pyfiles

    def _search_pyfiles(self, files):
        '''
        Search for python files in the prefix. This function doesn't do any
        real search, it only preprend the lib/Python$PYVERSION/site-packages/
        path to the given list of files
        '''
        pyfiles = []
        for f in files:
            f = f % self.extensions
            f = '%s/%s' % (self.py_prefix, f)
            real_name = self._pyfile_get_name(f)
            if real_name:
                pyfiles.append(real_name)
            else:
                # Adding it so we notice there is a problem in the recipe
                pyfiles.append(f)
            if f.endswith('.py'):
                cached_files = self._pyfile_get_cached(f)
                pyfiles.extend(cached_files)
        return pyfiles

    def _search_langfiles(self, files):
        '''
        Search for translations in share/locale/*/LC_MESSAGES/ '
        '''
        pattern = 'share/locale/*/LC_MESSAGES/%s.mo'
        return shell.ls_files([pattern % x for x in files],
                              self.config.prefix)

    def _search_typelibfiles(self, files):
        '''
        Search for typelibs in lib/girepository-1.0/
        '''
        if not self.config.variants.gi:
            return []

        pattern = 'lib/girepository-1.0/%s.typelib'
        typelibs = shell.ls_files([pattern % x for x in files],
                                  self.config.prefix)
        if not typelibs:
            # Add the architecture for universal builds
            pattern = 'lib/%s/girepository-1.0/%%s.typelib' % \
                self.config.target_arch
            typelibs = shell.ls_files([pattern % x for x in files],
                                      self.config.prefix)
        return typelibs

    def _search_girfiles(self):
        '''
        Search for typelibs in lib/girepository-1.0/
        '''
        if not self.config.variants.gi:
            return []

        girs = []
        if hasattr(self, 'files_' + self.TYPELIB_CAT):
            girs += getattr(self, 'files_' + self.TYPELIB_CAT)
        if hasattr(self, 'platform_files_' + self.TYPELIB_CAT):
            d = getattr(self, 'platform_files_' + self.TYPELIB_CAT)
            girs += d.get(self.config.target_platform, [])

        # Use a * for the arch in universal builds
        pattern = 'share/gir-1.0/%s.gir'
        files = shell.ls_files([pattern % x for x in girs],
                              self.config.prefix)
        if not girs:
            # Add the architecture for universal builds
            pattern = 'share/gir-1.0/%s/%%s.gir' % \
                self.config.target_arch
            files = shell.ls_files([pattern % x for x in girs],
                                      self.config.prefix)
        return files

    def _search_devel_libraries(self):
        devel_libs = []
        for category in self.categories:
            if category != self.LIBS_CAT and \
               not category.startswith(self.LIBS_CAT + '_'):
                continue

            pattern = 'lib/%(f)s.a lib/%(f)s.la '
            if self.platform == Platform.LINUX:
                pattern += 'lib/%(f)s.so '
            elif self.platform == Platform.WINDOWS:
                # MinGW import library
                pattern += 'lib/%(f)s.dll.a '
                # Module definitions (symbols) file
                pattern += 'lib/%(f)s.def '
                # MSVC import library
                pattern += 'lib/%(fnolib)s.lib '
                # MSVC Program database (debugging symbols) file
                # FIXME: Does not search for exe pdb files
                pattern += 'bin/%(fnolib)s*.pdb '
            elif self.platform in [Platform.DARWIN, Platform.IOS]:
                pattern += 'lib/%(f)s*.dylib '

            libsmatch = [pattern % {'f': x, 'fnolib': x[3:]} for x in
                         self._get_category_files_list(category)]
            devel_libs.extend(shell.ls_files(libsmatch, self.config.prefix))
        return devel_libs

    def _ls_dir(self, dirpath):
        files = []
        for root, dirnames, filenames in os.walk(dirpath):
            _root = root.split(self.config.prefix)[1]
            if _root[0] == '/':
                _root = _root[1:]
            files.extend([os.path.join(_root, x) for x in filenames])
        return files

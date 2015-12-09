#! /usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import with_statement, absolute_import, print_function

import os
import re
import shutil
import subprocess
import chardet

from flask            import current_app
from flask.ext.script import Manager

from .extensions import postprocess, preprocess
from ._compat    import urlopen, URLError, HTTPError

manager = Manager(usage="Asset bundling")


@manager.command
def bundle_assets():
    """Compress and minify assets"""
    YUI_COMPRESSOR_BIN = current_app.config.get('YUI_COMPRESSOR_BIN')
    path_to_jar = YUI_COMPRESSOR_BIN
    tmp_files = []

    def get_path(item):
        """Get the static path of an item"""
        return os.path.join(current_app.static_folder, item)

    def read_file(filename):
        content = ''
        path    = get_path(filename)
        with open(path, 'r', encoding='utf-8', errors='surrogateescape') as file:
            content = file.read().encode('utf-8', errors='surrogateescape')
        return content.decode('unicode_escape', errors='surrogateescape')

    def write_file(filename, content, compressed_file):
        out_file = get_path(os.path.join(current_app.config.get('BUNDLES_DIR'), 'tmp', '%s.tmp' % filename))
        if not os.path.exists(os.path.dirname(out_file)):
            os.makedirs(os.path.dirname(out_file))
        with open(out_file, 'w', encoding='utf-8', errors='surrogateescape') as file:
            file.write(content)
        return os.path.relpath(out_file, get_path('.'))

    def remove_comments(string):
        pattern = r"(\".*?\"|\'.*?\')|(/\*.*?\*/|//[^\r\n]*$)"
        # first group captures quoted strings (double or single)
        # second group captures comments (//single-line or /* multi-line */)
        regex = re.compile(pattern, re.MULTILINE|re.DOTALL)
        def _replacer(match):
            # if the 2nd group (capturing comments) is not None,
            # it means we have captured a non-quoted (real) comment string.
            if match.group(2) is not None:
                return "" # so we will return empty to remove the comment
            else: # otherwise, we will return the 1st group
                return match.group(1) # captured quoted-string
        return regex.sub(_replacer, string)

    def fix_urls_regex(url, filename, compressed_file):
        """Callback to fix relative path"""
        url = url.group(1).strip('"\'')
        if not url.startswith(('//', 'data:', 'http:', 'https:', 'attr(')):
            url = os.path.join(os.path.dirname(filename), url)
            url = os.path.relpath(get_path(url), get_path(os.path.dirname(compressed_file)))
        return "url(\'%s\')" % url

    def prepare_css(filename, compressed_file):
        """Fix relative paths in URLs for bundles and remove comments"""
        print("Fixing URL's in %s" % filename)
        parse   = lambda url: fix_urls_regex(url, filename, compressed_file)
        content = read_file(filename)
        content = re.sub('url\(([^)]*?)\)', parse, content)
        content = remove_comments(content)
        return write_file(filename, content, compressed_file)

    def preprocess_file(filename, compressed_file):
        """Preprocess the file"""
        if filename.startswith('//'):
            url = 'http:%s' % filename
        elif filename.startswith(('http:', 'https:')):
            url = filename
        else:
            url = None

        if url:
            ext_media_path = get_path('external')
            filename       = os.path.basename(url)
            if not os.path.exists(ext_media_path):
                os.makedirs(ext_media_path)
            if filename.endswith(('.js', '.css', '.less')):
                file_path = os.path.join(ext_media_path, filename)
                filename  = os.path.join('external', filename)
                assert('external' in ext_media_path)
                assert('external' in file_path)

                try:
                    req = urlopen(url)
                    print(' - Fetching %s ...' % url)
                except HTTPError as e:
                    print(' - HTTP Error %s for %s, %s' % (url, filename, str(e.code)))
                    return None
                except URLError as e:
                    print(' - Invalid URL %s for %s, %s' % (url, filename, str(e.reason)))
                    return None
                try:
                    print(' - Copying %s to %s ...' % (url, file_path))
                    with open(file_path, 'wb') as fp:
                        shutil.copyfileobj(req, fp)
                except shutil.Error:
                    print(' - Could not copy file %s' % filename)
            else:
                print(' - Not a valid remote file %s' % filename)
                return None
        else:
            try:
                if filename.endswith('.css'):
                    filename = prepare_css(filename, compressed_file)
                    tmp_files.append(filename)
            except Exception as e:
                pass
        # Return path of file
        filename = preprocess(filename.lstrip('/'))
        return get_path(filename.lstrip('/'))

    def minify(ftype, file_in, file_out):
        """Minify the file"""
        if ftype == 'js' and 'UGLIFY_BIN' in current_app.config:
            o = {'method': 'UglifyJS',
                 'bin': current_app.config.get('UGLIFY_BIN')}
            subprocess.call("%s -o %s %s" % (o['bin'], file_out, file_in),
                            shell=True, stdout=subprocess.PIPE)
        elif ftype == 'css' and 'CLEANCSS_BIN' in current_app.config:
            o = {'method': 'clean-css',
                 'bin': current_app.config.get('CLEANCSS_BIN')}
            subprocess.call("%s -o %s %s" % (o['bin'], file_out, file_in),
                            shell=True, stdout=subprocess.PIPE)
        else:
            o = {'method': 'YUI Compressor',
                 'bin': current_app.config.get('JAVA_BIN')}
            variables = (o['bin'], path_to_jar, file_in, file_out)
            subprocess.call("%s -jar %s %s -o %s" % variables,
                            shell=True, stdout=subprocess.PIPE)

        print("Minifying %s (using %s)" % (file_in, o['method']))

    # Assemble bundles and process
    bundles = {
        'css': current_app.config.get('CSS_BUNDLES'),
        'js': current_app.config.get('JS_BUNDLES'),
    }

    for ftype, bundle in bundles.items():
        for name, files in bundle.items():
            concatenated_file = get_path(os.path.join(
                current_app.config.get('BUNDLES_DIR'), ftype,
                '%s-all.%s' % (name, ftype,)))
            compressed_file = get_path(os.path.join(
                current_app.config.get('BUNDLES_DIR'), ftype,
                '%s-min.%s' % (name, ftype,)))

            if not os.path.exists(os.path.dirname(concatenated_file)):
                os.makedirs(os.path.dirname(concatenated_file))

            all_files = []
            for fn in files:
                processed = preprocess_file(fn, compressed_file)
                print('Processed: %s' % processed)
                if processed is not None:
                    all_files.append(processed)
            # Concatenate
            if len(all_files) == 0:
                print("Warning: '%s' is an empty bundle." % bundle)
            for file in all_files:
                subprocess.call("cat %s >> %s" % (file, concatenated_file), shell=True)
                subprocess.call("echo -n '\n' >> %s" % concatenated_file, shell=True)
            # Minify
            minify(ftype, concatenated_file, compressed_file)
            # Post process
            postprocess(compressed_file, fix_path=False)
            # Remove concatenated file
            print('Remove concatenated file')
            os.remove(concatenated_file)

    # Cleanup
    print('Clean up temporary files')
    for file in tmp_files:
        try:
            os.remove(get_path(file))
            os.rmdir(os.path.dirname(get_path(file)))
        except OSError:
            pass

    try:
        os.rmdir(get_path(os.path.join(current_app.config.get('BUNDLES_DIR'),
                                       'tmp')))
    except OSError:
        pass

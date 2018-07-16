# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

DEPS = [
    'clang_tidy',
    'fuchsia',
    'recipe_engine/json',
    'recipe_engine/raw_io',
    'recipe_engine/step',
]


def RunSteps(api):
  api.clang_tidy.ensure_clang()
  checkout_result = api.fuchsia.checkout(
      manifest='manifest/minimal',
      remote='tools',
      patch_ref='abcdef0123456789abcdef0123456789abcdef01',
  )
  compile_commands = api.clang_tidy.gen_compile_commands(checkout_result)

  all_checks = api.clang_tidy.run('step one', 'path/to/file', compile_commands)
  one_check = api.clang_tidy.run('step two', 'other/path/to/file',
                                 compile_commands,
                                 ['-*', 'fuchsia-default-arguments'])


def GenTests(api):

  has_errors = '''- DiagnosticName:  'check'
  Message:         'error'
  FileOffset:      1
  FilePath:        'path/to/file'
'''
  has_errors_json = [{
      'FileOffset': 1,
      'DiagnosticName': 'check',
      'Message': 'error',
      'FilePath': 'path/to/file'
  }]

  yield (api.test('basic') + api.step_data(
      'step one.load yaml', stdout=api.json.output(has_errors_json)) +
         api.step_data('step two.load yaml', stdout=api.json.output('')))

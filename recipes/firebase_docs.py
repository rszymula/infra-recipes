# Copyright 2018 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Recipe for generating docs for upload to Firebase."""

from recipe_engine.recipe_api import Property

DEPS = [
    'infra/auto_roller',
    'infra/cipd',
    'infra/fuchsia',
    'infra/git',
    'infra/jiri',
    'recipe_engine/buildbucket',
    'recipe_engine/context',
    'recipe_engine/json',
    'recipe_engine/file',
    'recipe_engine/path',
    'recipe_engine/properties',
    'recipe_engine/raw_io',
    'recipe_engine/step',
]

PROPERTIES = {
    'dry_run':
        Property(
            kind=bool,
            help='Whether to upload docs to firebase.',
            default=False),
}

DARTDOC_PUBSPEC = """name: Fuchsia
homepage: fuchsia-docs.firebaseapp.com
description: API documentation for fuchsia
dependencies:
"""


def gen_dartdoc(api, out_dir, docs_dir, cipd_dir):
  '''Generate dartdoc output.

  Dartdoc runs on a single package, but has the capability to generate docs for all
  dependencies. Thus, to generate Dart documentation for Fuchsia, we first generate
  a 'fake' package that lists the libraries we want documented. We then run `pub`
  over that new package to fetch the dependencies, and finally `dartdoc` to generate
  documentation for it all.

  Args:
    out_dir (Path) - The output directory for generated files.
    docs_dir (Path) - The output directory for documentation.
    cipd_dir (Path) - The cipd directory.
  '''
  dart_packages_path = api.path['start_dir'].join('topaz', 'public', 'dart')
  api.path.mock_add_paths(dart_packages_path)
  # If either dartdoc or dart packages path doesn't exist, we didn't checkout topaz
  # and so we won't generate dart docs on this run.
  if not api.path.exists(dart_packages_path):
    return  # pragma: no cover

  # Make a temporary docs dir to be pushed to firebase.
  api.file.ensure_directory('create lib dir', out_dir.join('lib'))

  # Build .packages and lib.dart importing all packages.
  dart_imports_content = 'library Fuchsia;\n'
  dart_pubspec_content = DARTDOC_PUBSPEC

  # Gather documentable dart packages.
  dart_packages = [
      api.path.basename(p) for p in api.file.listdir(
          'list dart packages',
          dart_packages_path,
          test_data=('fuchsia', 'topaz'))
  ]

  api.path.mock_add_paths(dart_packages_path.join('fuchsia', 'lib'))

  for package in dart_packages:
    if not api.path.exists(dart_packages_path.join(package, 'lib')):
      continue

    # TODO(juliehockett): Remove this once the widgets library is standardized.
    if package == 'widgets':
      continue  # pragma: no cover

    dart_pubspec_content += ' %s:\n    path: %s/\n' % (
        package, dart_packages_path.join(package))
    package_imports = [
        api.path.basename(i)
        for i in api.file.listdir('list %s packages' % package,
                                  dart_packages_path.join(package, 'lib'))
        if api.path.basename(i).endswith('.dart')
    ]
    for i in package_imports:
      dart_imports_content += 'import \'package:%s/%s\';\n' % (package, i)

  # TODO(juliehockett): Remove this once we have a story for generated pubspec.yaml files.
  fidl_fuchsia_sys_path = api.path['start_dir'].join(
      'out', 'release-x64', 'dartlang', 'gen', 'garnet', 'public', 'fidl',
      'fuchsia.sys', 'fuchsia.sys_package')
  api.file.write_text(
      'write fidl_fuchsia_sys pubspec.yaml',
      fidl_fuchsia_sys_path.join('pubspec.yaml'), """name: fidl_fuchsia_sys
environment:
  sdk: '>=2.0.0 <3.0.0'
""")
  dart_pubspec_content += ' %s:\n    path: %s/\n' % ('fidl_fuchsia_sys',
                                                     fidl_fuchsia_sys_path)

  # Build package pubspec.yaml depending on all desired source packages.
  api.file.write_text('write pubspec.yaml', out_dir.join('pubspec.yaml'),
                      dart_pubspec_content)
  api.file.write_text('write lib.dart', out_dir.join('lib', 'lib.dart'),
                      dart_imports_content)

  # Run pub over this package to fetch deps.
  with api.context(cwd=out_dir):
    api.step('pub', [cipd_dir.join('dart-sdk', 'bin', 'pub'), 'get'])

  # Run dartdoc over this package.
  with api.context(cwd=out_dir):
    api.step('dartdoc', [
        cipd_dir.join('dart-sdk', 'bin', 'dartdoc'),
        '--auto-include-dependencies',
        '--exclude-packages',
        'fidl_fuchsia_sys',
        '--exclude-packages',
        'Dart',
        '--output',
        docs_dir.join('public', 'dart'),
    ])


def gen_rustdoc(api, out_dir, docs_dir, cipd_dir, build_dir):
  '''Generate rust output.

  The rustdoc script runs on GN targets. We find the Rust GN targets by finding all targets that generate a Cargo.toml file, and use those.

  Args:
    out_dir (Path) - The output directory for generated files.
    docs_dir (Path) - The output directory for documentation.
    cipd_dir (Path) - The cipd directory.
  '''
  step_result = api.step(
      'gn desc', [
          api.path['start_dir'].join('buildtools', 'gn'),
          'desc',
          build_dir,
          '//*',
          'outputs',
          '--type=action',
          '--format=json',
      ],
      stdout=api.json.output())

  skipped = []
  test_cargo_path = build_dir.join('target', 'Cargo.toml')
  api.path.mock_add_paths(test_cargo_path)
  for target in step_result.stdout:
    if not 'outputs' in step_result.stdout[target]:
      continue
    outputs = step_result.stdout[target]['outputs']
    # If the target doesn't output a Cargo.toml file, it's not a rust target.
    if not outputs[0].endswith('Cargo.toml'):
      continue

    output = api.path['start_dir'].join(*outputs[0].split('/'))

    if not api.path.exists(output):
      skipped.append(target)
      continue

    api.step('rustdoc %s' % target, [
        api.path['start_dir'].join('scripts', 'devshell', 'rustdoc'),
        output,
        "--no-deps",
        "--out-dir",
        build_dir,
    ])

  # Move the output to the docs directory.
  step_result = api.step('move output to docs', [
      'mv',
      api.path['start_dir'].join('out', 'cargo_target', 'x86_64-fuchsia',
                                 'doc'),
      docs_dir.join('public', 'rust'),
  ])

  step_result.presentation.logs['skipped'] = skipped


def push_to_firebase(api, cipd_dir, docs_dir):
  with api.context(cwd=docs_dir):
    api.step('firebase deploy', [
        cipd_dir.join('firebase-tools', 'bin', 'firebase'),
        'deploy',
    ])


def RunSteps(api, dry_run):
  api.jiri.ensure_jiri()

  cipd_dir = api.path['start_dir'].join('cipd')
  with api.step.nest('ensure_packages'):
    with api.context(infra_steps=True):
      api.cipd.ensure(
          cipd_dir, {
              'infra/nodejs/nodejs/${platform}': 'latest',
              'dart/dart-sdk/${platform}': 'dev',
          })

  # firebase-tools expects to live in the node_modules subdir of where nodejs is installed.
  node_modules_dir = cipd_dir.join('node_modules')
  with api.step.nest('ensure_packages'):
    with api.context(infra_steps=True):
      api.cipd.ensure(node_modules_dir, {
          'infra/npm/firebase-tools': 'latest',
      })

  resources_dir = api.path['start_dir'].join('api-docs-resources')
  api.git.checkout(
      'https://fuchsia.googlesource.com/api-docs-resources', path=resources_dir)

  api.fuchsia.checkout(
      manifest='manifest/topaz',
      remote='https://fuchsia.googlesource.com/topaz',
      project='topaz',
      build=api.buildbucket.build,
  )

  build = api.fuchsia.build(
      target='x64',
      build_type='release',
      packages=['topaz/packages/default'],
  )

  out_dir = api.path['start_dir'].join('docs_out')
  docs_dir = api.path['start_dir'].join('firebase')

  api.file.rmtree('remove old docs', docs_dir)
  api.file.copytree('copy resources', resources_dir, docs_dir)

  with api.step.nest('dartdoc'):
    gen_dartdoc(api, out_dir, docs_dir, cipd_dir)
  with api.step.nest('rustdoc'):
    gen_rustdoc(api, out_dir, docs_dir, cipd_dir, build.fuchsia_build_dir)

  if not dry_run:
    push_to_firebase(api, node_modules_dir, docs_dir)


def GenTests(api):

  yield (api.test('firebase_docs') + api.buildbucket.ci_build(
      git_repo='https://fuchsia.googlesource.com/topaz',) + api.step_data(
          'dartdoc.list fuchsia packages', api.file.listdir(['fuchsia.dart'])) +
         api.step_data(
             'rustdoc.gn desc',
             stdout=api.json.output({
                 "//topaz/target:target_cargo": {
                     "outputs": ["//out/release-x64/target/Cargo.toml"]
                 },
                 "//topaz/not_target:not_target": {
                     "outputs": [
                         "//out/release-x64/not_target.h",
                         "//out/release-x64/not_target.cc"
                     ]
                 },
                 "//topaz/target:missing_outputs": {
                     "outputs": ["//out/release-x64/missing/Cargo.toml"]
                 },
                 "//topaz/not_target:no_outputs": {}
             })))

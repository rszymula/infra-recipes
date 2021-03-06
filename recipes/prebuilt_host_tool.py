# Copyright 2018 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Recipe for building and publishing CIPD prebuilts"""

from recipe_engine.config import List
from recipe_engine.recipe_api import Property

DEPS = [
    'infra/fuchsia',
    'infra/jiri',
    'recipe_engine/buildbucket',
    'recipe_engine/cipd',
    'recipe_engine/context',
    'recipe_engine/json',
    'recipe_engine/properties',
    'recipe_engine/step',
]

PROPERTIES = {
    'cipd_pkg_prefix':
        Property(
            kind=str,
            help='The CIPD prefix where the tool binaries should be uploaded',
            default=None),
    'manifest':
        Property(kind=str, help='Jiri manifest to use'),
    'ninja_targets':
        Property(
            kind=List(basestring),
            help='Extra target args to pass to ninja',
            default=[]),
    'packages':
        Property(kind=List(basestring), help='Packages to build', default=[]),
    'project':
        Property(kind=str, help='Jiri remote manifest project', default=None),
    'remote':
        Property(kind=str, help='Remote manifest repository'),
}


# TODO(IN-581): Extract common UploadPackage functionality to api.cipd
def UploadPackage(api, bin_dir, bin_name, cipd_pkg_prefix, revision, remote):
  """Creates and uploads a CIPD package containing the tool at '$bin_dir/$bin_name'.

  The tool is published to CIPD under the path '$cipd_pkg_prefix/$bin_name/$platform'

  Args:
    api: The RecipeApi object
    bin_dir: The absolute path to the parent directory of bin_name.
    bin_name: The name of the tool binary
    cipd_pkg_prefix: The CIPD package prefix where the tool binary should be uploaded
    revision: The revision at which the tool binary was built
    remote: The git remote where code for the tool binary lives
  """

  cipd_pkg_name = '%s/%s/${platform}' % (cipd_pkg_prefix, bin_name)
  pins = api.cipd.search(cipd_pkg_name, 'git_revision:' + revision)
  if len(pins) > 0:
    api.step('Package is up-to-date', cmd=None)
    return

  pkg_def = api.cipd.PackageDefinition(
      package_name=cipd_pkg_name,
      package_root=bin_dir,
  )
  pkg_def.add_file(bin_dir.join(bin_name))

  api.cipd.create_from_pkg(
      pkg_def,
      refs=['latest'],
      tags={
          'git_repository': remote,
          'git_revision': revision,
      },
  )


def GetBinPathComponents(build_dir, ninja_target):
  """Returns a binary's path based on its ninja target.

  Args:
    build_dir: The Fuchsia build output directory
    ninja_target: A specific Ninja target that was built

  Returns:
    bin_dir: The absolute path to the parent directory of bin_name.
    bin_name: The name of the tool binary
  """
  parts = ninja_target.split('/')
  bin_dir = build_dir.join(*parts[:-1])
  bin_name = parts[-1]
  return bin_dir, bin_name


def RunSteps(api, cipd_pkg_prefix, manifest, ninja_targets, packages,
             project, remote):

  with api.context(infra_steps=True):
    assert manifest
    assert remote
    build = api.buildbucket.build
    api.fuchsia.checkout(
        manifest=manifest,
        remote=remote,
        project=project,
        build=build)

    revision = str(build.input.gitiles_commit.id)
    assert revision

  # TODO(IN-580): Extract ninja build functionality into its own recipe_module
  build = api.fuchsia.build(
      target='x64',
      build_type='release',
      packages=packages,
      ninja_targets=ninja_targets)

  if not api.properties.get('tryjob', False):
    for ninja_target in ninja_targets:
      bin_dir, bin_name = GetBinPathComponents(build.fuchsia_build_dir,
                                               ninja_target)
      UploadPackage(api, bin_dir, bin_name, cipd_pkg_prefix, revision, remote)


def GenTests(api):
  revision = api.jiri.example_revision
  ci_build = api.buildbucket.ci_build(
    git_repo='https://fuchsia.googlesource.com/build',
    revision=revision,
  )

  yield api.test('default') + ci_build + api.properties(
      cipd_pkg_prefix='fuchsia/tools',
      manifest='manifest/build',
      ninja_targets=['tools/json_validator'],
      packages=['build/packages/json_validator'],
      project='build',
      remote='https://fuchsia.googlesource.com/build',
  ) + api.step_data(
      # Mock api.cipd.search(cipd_pkg_name, 'git_revision:' + revision)
      # by expanding the internal step name and providing a result for the step
      'cipd search fuchsia/tools/json_validator/${platform} git_revision:%s' % revision,
      api.json.output({
          'result': []
      }),
  )
  yield api.test('cipd_has_revision') + ci_build + api.properties(
      cipd_pkg_prefix='fuchsia/tools',
      manifest='manifest/build',
      ninja_targets=['tools/json_validator'],
      packages=['build/packages/json_validator'],
      project='build',
      remote='https://fuchsia.googlesource.com/build',
  ) + api.step_data(
      'cipd search fuchsia/tools/json_validator/${platform} git_revision:%s' % revision,
      api.cipd.example_search('fuchsia/tools/json_validator/${platform}:%s' % revision),
  )
  yield api.test('no_revision') + ci_build + api.properties(
      cipd_pkg_prefix='fuchsia/tools',
      manifest='manifest/build',
      ninja_targets=['tools/json_validator'],
      packages=['build/packages/json_validator'],
      project='build',
      remote='https://fuchsia.googlesource.com/build',
  )

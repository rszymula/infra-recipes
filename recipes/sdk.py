# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Recipe for building Fuchsia SDKs."""

from contextlib import contextmanager

from recipe_engine.config import Enum
from recipe_engine.recipe_api import Property

import collections

DEPS = [
    'infra/bazel',
    'infra/cipd',
    'infra/fuchsia',
    'infra/go',
    'infra/gsutil',
    'infra/hash',
    'infra/jiri',
    'infra/tar',
    'recipe_engine/buildbucket',
    'recipe_engine/context',
    'recipe_engine/file',
    'recipe_engine/json',
    'recipe_engine/path',
    'recipe_engine/platform',
    'recipe_engine/properties',
    'recipe_engine/python',
    'recipe_engine/step',
]

REPOS = ['garnet', 'topaz']

BUILD_TYPE = 'release'

PROPERTIES = {
    'project':
        Property(kind=str, help='Jiri remote manifest project'),
    'manifest':
        Property(kind=str, help='Jiri manifest to use'),
    'remote':
        Property(kind=str, help='Remote manifest repository'),
    'repo':
        Property(kind=Enum(*REPOS), help='Repo to checkout, build', default=None),
}

def RunSteps(api, project, manifest, remote, repo):
  api.go.ensure_go()
  api.gsutil.ensure_gsutil()

  build = api.buildbucket.build
  api.fuchsia.checkout(
      build=build,
      manifest=manifest,
      remote=remote,
      project=project)

  revision = build.input.gitiles_commit.id
  global_integration = 'global' in build.builder.bucket

  # Build fuchsia for each target.
  builds = {}
  for target in ('arm64', 'x64'):
    with api.step.nest('build ' + target):
      sdk_build_package = '%s/packages/sdk/%s' % (repo, repo)
      builds[target] = api.fuchsia.build(
          target=target,
          build_type=BUILD_TYPE,
          packages=[sdk_build_package],
          gn_args=['build_sdk_archives=true'])

  # Merge the SDK archives for each target into a single archive.
  # Note that "alpha" and "beta" below have no particular meaning.
  merge_path = api.path['start_dir'].join('scripts', 'sdk', 'merger',
                                          'merge.py')
  full_archive_path = api.path['cleanup'].join('merged_sdk_archive.tar.gz')
  api.python('merge sdk archives',
      merge_path,
      args=[
        '--alpha-archive',
        builds['x64'].fuchsia_build_dir.join('sdk', 'archive', '%s.tar.gz' %
                                             repo),
        '--beta-archive',
        builds['arm64'].fuchsia_build_dir.join('sdk', 'archive', '%s.tar.gz' %
                                               repo),
        '--output-archive',
        full_archive_path,
      ])

  # Generate a Bazel workspace along with its tests.
  # These tests are being run for every SDK type.
  scripts_path = api.path['start_dir'].join('scripts', 'sdk', 'bazel')
  sdk_dir = api.path['cleanup'].join('sdk-bazel')
  test_workspace_dir = api.path['cleanup'].join('tests')

  api.python('create bazel sdk',
      scripts_path.join('generate.py'),
      args=[
        '--archive',
        full_archive_path,
        '--output',
        sdk_dir,
        '--tests',
        test_workspace_dir,
      ],
  )

  with api.step.nest('test sdk'):
    bazel_path = api.bazel.ensure_bazel()

    api.python('run tests',
        test_workspace_dir.join('run.py'),
        args=[
          '--bazel',
          bazel_path,
        ],
    )

  # Only publish the resulting Bazel SDK for Topaz.
  if repo == 'topaz':
    # TODO(nsylvain): Remove restriction on uploading only for global
    # integration CI once Bazel SDK consumers can differentiate between local
    # and global uploads.
    if revision and global_integration:
      with api.step.nest('upload bazel sdk'):
        # Upload the SDK to CIPD and GCS.
        UploadPackage(api, 'bazel', sdk_dir, remote, revision)

  # Publish the unprocessed SDK for Garnet.
  if repo == 'garnet':
    sdk_dir = api.path['cleanup'].join('chromium-sdk')

    # Extract the archive to a directory for CIPD processing.
    with api.step.nest('extract chromium sdk'):
      api.file.ensure_directory('create sdk dir', sdk_dir)
      api.tar.ensure_tar()
      api.tar.extract(
          step_name='unpack sdk archive',
          path=full_archive_path,
          directory=sdk_dir,
      )

    if revision:
      with api.step.nest('upload chromium sdk'):
        # Upload the Chromium style SDK to GCS and CIPD. Only upload digest
        # during global integration CI.
        UploadArchive(api, full_archive_path, sdk_dir, remote, revision,
                      upload_digest=global_integration)

# Given an SDK |sdk_name| with artifacts found in |staging_dir|, upload a
# corresponding .cipd file to CIPD and GCS.
def UploadPackage(api, sdk_name, staging_dir, remote, revision):
  sdk_subpath = 'sdk/%s/%s' % (sdk_name,  api.cipd.platform_suffix())
  cipd_pkg_name = 'fuchsia/%s' % sdk_subpath
  step = api.cipd.search(cipd_pkg_name, 'git_revision:' + revision)
  if step.json.output['result']:
    api.step('Package is up-to-date', cmd=None)
    return

  pkg_def = api.cipd.PackageDefinition(
      package_name=cipd_pkg_name,
      package_root=staging_dir,
      install_mode='copy')
  pkg_def.add_dir(staging_dir)
  pkg_def.add_version_file('.versions/sdk.cipd_version')

  cipd_pkg_file = api.path['cleanup'].join('sdk.cipd')

  api.cipd.build_from_pkg(
      pkg_def=pkg_def,
      output_package=cipd_pkg_file,
  )
  step_result = api.cipd.register(
      package_name=cipd_pkg_name,
      package_path=cipd_pkg_file,
      refs=['latest'],
      tags={
        'git_revision': revision,
      }
  )

  instance_id = step_result.json.output['result']['instance_id']
  api.gsutil.upload(
      'fuchsia',
      cipd_pkg_file,
      api.gsutil.join(sdk_subpath, instance_id),
      unauthenticated_url=True
  )


def UploadArchive(api, sdk, out_dir, remote, revision, upload_digest):
  digest = api.hash.sha1('hash archive', sdk)
  archive_base_location = 'sdk/' + api.cipd.platform_suffix()
  archive_bucket = 'fuchsia'
  api.gsutil.upload(
      bucket=archive_bucket,
      src=sdk,
      dst='%s/%s' % (archive_base_location, digest),
      link_name='archive',
      name='upload fuchsia-sdk %s' % digest,
      unauthenticated_url=True)
  # Note that this will upload the snapshot to a location different from the
  # path that api.fuchsia copied it to. This uses a path based on the hash of
  # the SDK artifact, not based on the hash of the snapshot itself. Clients can
  # use this to find the snapshot used to build a specific SDK artifact.
  snapshot_file = api.path['cleanup'].join('jiri.snapshot')
  api.jiri.snapshot(snapshot_file)
  api.gsutil.upload(
      bucket='fuchsia-snapshots',
      src=snapshot_file,
      dst=digest,
      link_name='jiri.snapshot',
      name='upload jiri.snapshot',
      unauthenticated_url=True)

  if upload_digest:
    # Record the digest of the most recently uploaded archive for downstream autorollers.
    digest_path = api.path['cleanup'].join('digest')
    api.file.write_text('write digest', digest_path, digest)
    api.gsutil.upload(
        bucket=archive_bucket,
        src=digest_path,
        dst='%s/LATEST_ARCHIVE' % archive_base_location,
        link_name='LATEST_ARCHIVE',
        name='upload latest digest',
        unauthenticated_url=True)

  # Upload the SDK to CIPD as well.
  cipd_pkg_name = 'fuchsia/sdk/' + api.cipd.platform_suffix()
  step = api.cipd.search(cipd_pkg_name, 'git_revision:' + revision)
  if step.json.output['result']:
    api.step('Package is up-to-date', cmd=None)
    return

  pkg_def = api.cipd.PackageDefinition(
      package_name=cipd_pkg_name,
      package_root=out_dir,
      install_mode='copy')
  pkg_def.add_dir(out_dir)

  api.cipd.create_from_pkg(
      pkg_def=pkg_def,
      refs=['latest'],
      tags={
        'git_revision': revision,
        'jiri_snapshot': digest,
      }
  )


# yapf: disable
def GenTests(api):
  revision = api.jiri.example_revision
  garnet_properties = api.properties(
      project='integration',
      repo='garnet',
      manifest='fuchsia/garnet/garnet',
      remote='https://fuchsia.googlesource.com/integration')
  topaz_properties = api.properties(
      project='integration',
      repo='topaz',
      manifest='fuchsia/topaz/topaz',
      remote='https://fuchsia.googlesource.com/integration')

  garnet_local_ci = (garnet_properties +
    api.buildbucket.ci_build(
      git_repo="https://fuchsia.googlesource.com/garnet",
      revision=revision,
    ) +
    api.step_data('upload chromium sdk.hash archive', api.hash(revision))
  )
  garnet_global_ci = (garnet_properties +
    api.buildbucket.ci_build(
      git_repo="https://fuchsia.googlesource.com/garnet",
      revision=revision,
      bucket="###global-integration-bucket###"
    ) +
    api.step_data('upload chromium sdk.hash archive', api.hash(revision))
  )

  topaz_local_ci = topaz_properties + api.buildbucket.ci_build(
    git_repo="https://fuchsia.googlesource.com/topaz",
    revision=revision,
  )
  topaz_global_ci = topaz_properties + api.buildbucket.ci_build(
    git_repo="https://fuchsia.googlesource.com/topaz",
    revision=revision,
    bucket="###global-integration-bucket###"
  )

  yield (api.test('garnet_local_ci') + garnet_local_ci)
  yield (api.test('garnet_global_ci') + garnet_global_ci)
  yield (api.test('garnet_global_ci_new_upload') +
      garnet_global_ci +
      api.step_data('upload chromium sdk.cipd search fuchsia/sdk/linux-amd64 ' +
                    'git_revision:%s' % revision,
                     api.json.output({'result': []}))

  )
  yield (api.test('topaz_local_ci') + topaz_local_ci)
  yield (api.test('topaz_global_ci') + topaz_global_ci)
  yield (api.test('topaz_global_ci_new_upload') +
      topaz_global_ci +
      api.step_data('upload bazel sdk.cipd search fuchsia/sdk/bazel/linux-amd64 ' +
                  'git_revision:%s' % revision,
                   api.json.output({'result': []}))

  )
  yield (api.test('cq') +
      api.buildbucket.try_build(
        git_repo="https://fuchsia.googlesource.com/topaz",
      ) +
      topaz_properties
  )
# yapf: enable

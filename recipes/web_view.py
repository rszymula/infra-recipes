# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Recipe for building libwebkit.so."""

from recipe_engine.config import Enum, List
from recipe_engine.recipe_api import Property

DEPS = [
    'infra/fuchsia',
    'infra/gitiles',
    'infra/gsutil',
    'infra/hash',
    'infra/jiri',
    'recipe_engine/buildbucket',
    'recipe_engine/context',
    'recipe_engine/properties',
    'recipe_engine/step',
]

TARGETS = ['arm64', 'x64']


def RunSteps(api):
  api.gitiles.ensure_gitiles()

  build = api.buildbucket.build
  revision = build.input.gitiles_commit.id

  checkout = api.fuchsia.checkout(
      # jiri manifest lives in fuchsia/manifests, if this
      # is a CI build, we just want to checkout at HEAD
      build=None if revision else build,
      manifest='webkit',
      remote='https://fuchsia.googlesource.com/manifest',
  )

  # For historical reasons, webview prebuilts use a hash of the snapshot file
  # as a GCS path component: this ensured that the binaries are versioned by
  # the code used to build them, even before all manifest entries were pinned.
  # Now that all manifest entries are pinned, this could be replaced with a
  # git-revision-based path like ffmpeg.py uses.
  snapshot_file_sha1 = api.hash.sha1(
      'hash jiri.snapshot',
      checkout.snapshot_file,
      test_data='cd963da3f17c3acc611a9b9c1b272fcd6ae39909')

  # Build for all targets before uploading any to avoid an incomplete upload.
  builds = {}  # keyed by target string
  for target in TARGETS:
    with api.step.nest('build ' + target):
      builds[target] = api.fuchsia.build(
          target=target,
          build_type='release',
          packages=['third_party/webkit/packages/webkit'],
          ninja_targets=['third_party/webkit'],
      )

  # If this isn't a real run, don't pollute the storage.
  if api.properties.get('tryjob'):
    return

  # Upload the built library to Google Cloud Storage.
  # api.fuchsia.checkout() doesn't always ensure that gsutil exists.
  api.gsutil.ensure_gsutil()

  for target in TARGETS:
    with api.step.nest('upload ' + target):
      # The GCS path has three main components:
      # - target architecture
      # - third_party/webkit git HEAD hash
      # - topaz jiri.snapshot file hash
      # The HEAD hash component lets us find all builds for a given version
      # of the webkit code. But, since the actual binary includes pieces
      # of topaz (header-defined values, static libs, etc.), it's also
      # important to reflect the topaz version.
      bucket_root = {'arm64': 'aarch64', 'x64': 'x86_64'}[target]
      build_dir = builds[target].fuchsia_build_dir
      api.gsutil.upload(
          bucket='fuchsia',
          src=build_dir.join('%s-shared' % target, 'libwebkit.so'),
          dst=api.gsutil.join(bucket_root, 'webkit', revision,
                              snapshot_file_sha1, 'libwebkit.so'),
          link_name='libwebkit.so',
          unauthenticated_url=True,
          name='upload libwebkit.so',
      )


def GenTests(api):
  yield api.fuchsia.test(
      'ci',
      clear_default_properties=True,
  )
  yield api.fuchsia.test(
      'cq',
      clear_default_properties=True,
      tryjob=True,
  )

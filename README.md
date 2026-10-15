# iStoreOS 25.12 x86_64 EFI builder

GitHub Actions pipeline that compiles the iStoreOS 25.12 x86_64 EFI firmware
from source, preinstalls the OTA updater and the homeproxy / PassWall / Nikki /
mosdns plugins, publishes a rolling GitHub release and refreshes an OTA manifest on
the `gh-pages` branch.

## Layout

```
.github/workflows/build-istoreos.yml   workflow: disk, cache, stage invocation
scripts/build_istoreos.py              full pipeline orchestration (Python)
config/x86_64_efi.config               seed: target + image/layout settings
config/x86_64_efi.packages             seed: package selection
config/feeds.conf                      feed list (upstream baseline + plugins)
config/feeds.remove                    feed name-conflict resolution table
```

The workflow itself only prepares the runner (disk expansion, checkout,
cache). All real work is driven by `scripts/build_istoreos.py`, which can also
be run locally for debugging.

## Pipeline stages

Run the whole thing with `python3 scripts/build_istoreos.py all`, or a single
stage for debugging (`deps`, `source`, `patch`, `feeds`, `config`, `build`,
`collect`, `release`, `ota`).

1. `deps` - install the OpenWrt build dependencies with `apt-get`.
2. `source` - shallow clone `istoreos/istoreos` branch `istoreos-25.12`.
3. `patch` - source customisations (see below).
4. `feeds` - write `feeds.conf`, `feeds update -a`, resolve name conflicts,
   `feeds install -a`.
5. `config` - concatenate the seed + package list into `.config`, drop an
   OTA `api_url` uci-defaults overlay, run `make defconfig`.
6. `build` - `make download` then `make -j<N>` with `SOURCE_DATE_EPOCH` set.
7. `collect` - copy the combined-EFI image to `out/`, compute sha256, write
   `out/artifact-index.json`.
8. `release` - create or update the rolling release `v25.12.5` via `gh`.
9. `ota` - generate `ota.json` and force-push it to `gh-pages`.

## Source customisations (stage `patch`)

- **Default LAN IP `10.0.0.1`**: iStoreOS rewrites the upstream defaults at
  package-install time from a `sed` inside `package/istoreos-files/Makefile`,
  so retargeting that `sed` moves the default for the login page,
  `config_generate` and the flash page at once.
- **`BUILD_DATE`**: the sbwml OTA updater reads a plain `BUILD_DATE` variable
  from `/etc/os-release`; the upstream template only exposes
  `OPENWRT_BUILD_DATE`. A `BUILD_DATE="%B"` line is appended and
  `%B` is filled from `SOURCE_DATE_EPOCH`, which the build stage sets to the
  build timestamp.
- **OTA replacement**: the in-tree `package/diy/luci-app-ota` is moved to the
  quarantine dir so the sbwml `luci-app-ota` + `otahelper` from the
  `openwrt_pkgs` feed is the only provider.

## Seed provenance

There is no published `config.seed` for the 25.12 x86_64 EFI build (the
KoolCenter imagebuilder under `ib/` is a stale 22.03 / kernel 5.10 tree). The
package list in `config/x86_64_efi.packages` is therefore reconstructed from
the official 25.12 manifest
(`fw.koolcenter.com/iStoreOS/ib/x86_64/istoreos-x86-64-generic.manifest`) plus
the plugin additions. `make defconfig` resolves the rest.

## Feed conflict handling

The upstream `packages`/`luci` feeds ship copies of packages that the plugin
feeds also provide. `config/feeds.remove` maps each conflicting package to the
feed that should own it; the script moves every other copy into
`work/istoreos/quarantine` before `feeds install`.

The indexes written by `feeds update` are built *before* that quarantine, so
they still advertise the removed packages. `feeds install` resolves a source
package by scanning those indexes in `feeds.conf` order, so it would pick the
now-empty baseline feed, create a dangling symlink and mark the name as
already installed, leaving the intended provider feed uninstalled (surfacing as
`... has a dependency on 'sing-box', which does not exist`). Stage `feeds`
therefore runs `feeds update -i` to rebuild the indexes after the quarantine.

A provider of `-` drops the package from every feed instead of picking a
winner. This removes packages that break the build or that nothing selected
depends on:

- `mihomo-alpha`: `mihomo-meta` and `mihomo-alpha` `CONFLICT` with each other
  and both `PROVIDES` the virtual `mihomo` symbol, so kconfig reports a
  recursive dependency. `mihomo-meta` is kept as the stable variant.
- `luci-app-ssr-plus`: its conditional engine dependencies
  (`+PACKAGE_luci-app-ssr-plus_INCLUDE_X:y`, where the engine has arch
  restrictions) make kconfig report a recursive dependency per engine. It is
  not selected by any seed, so it is dropped; its engines are still pulled by
  `luci-app-passwall` where needed.
- `luci-app-baidupcs-web`: depends on a `baidupcs-web` backend that no feed
  ships, which makes the package scanner warn on every build.

## OTA manifest

`ota.json` on `gh-pages` follows the sbwml format, keyed by device
(`x86_64`), with `build_date`, `sha256sum`, `url` and `logs`. The firmware's
`/etc/config/ota` `api_url` is seeded to
`https://raw.githubusercontent.com/<owner>/<repo>/gh-pages/ota.json`.

## Required configuration

- Repository permission for the workflow token: `contents: write` (already set
  in the workflow) so it can create the release and push `gh-pages`.
- No extra secrets: `GITHUB_TOKEN` is provided by Actions as `GH_TOKEN`.

## Local run

```bash
ISTOREOS_BUILD_ROOT=/path/with/space python3 scripts/build_istoreos.py deps
ISTOREOS_BUILD_ROOT=/path/with/space python3 scripts/build_istoreos.py all
```

Disk expansion is a CI-only concern; locally ensure enough free space for the
OpenWrt build tree (30-50 GiB recommended).

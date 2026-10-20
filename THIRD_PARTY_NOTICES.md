# Third-party notices

AgentStrata's original code and documentation use **AGPL-3.0-only**; see
[COPYRIGHT](COPYRIGHT). Third-party works retain their original copyright,
licenses and notices and are not relicensed by this file.

## Repository inventory

The repository includes the following copied source and benchmark samples:

| Material | License and attribution | Provenance and modifications |
| --- | --- | --- |
| IFEval scorer | Apache-2.0; Google Research authors | [Pinned revision, checksums and changes](src/chatcopilot/evals/vendor/ifeval/NOTICE.md); [license](src/chatcopilot/evals/vendor/ifeval/LICENSE.txt) |
| BFCL scorer core | Apache-2.0; UC Berkeley Gorilla / BFCL authors | [Pinned revision, copied functions and changes](src/chatcopilot/evals/vendor/bfcl/NOTICE.md); [license](src/chatcopilot/evals/vendor/bfcl/LICENSE.txt) |
| IFEval fixed fixtures | Apache-2.0; Google Research authors | [Fixtures](src/chatcopilot/evals/suites/ifeval/fixtures); [license](src/chatcopilot/evals/suites/ifeval/fixtures/LICENSE.txt) |
| IFEval capability subset | Apache-2.0; Copyright 2023 The Google Research Authors | [Eight-record subset and adaptation notice](src/chatcopilot/evals/suites/agentstrata-capabilities-v1/IFEVAL-NOTICE.md); [license](src/chatcopilot/evals/suites/agentstrata-capabilities-v1/fixtures/IFEVAL-LICENSE.txt) |

Preserve original headers, local licenses and modification notices. Benchmark
software licensing does not automatically license its datasets or task assets.
Separately prepared GAIA, SWE-bench, WebArena and AgentBench resources remain
subject to their exact dataset, task-repository and service terms.

The repository declares dependencies and optional runtime services that are
downloaded separately:

- Python runtime dependencies are declared in `pyproject.toml`.
- The release-only, hash-locked Python build closure is declared in
  `requirements/release-build.txt`; those tools are downloaded during CI
  and are not redistributed by AgentStrata.
- Console JavaScript dependencies are declared in `console/web/package.json`
  and locked by `console/web/package-lock.json`.
- Optional OCI images and MCP packages are declared in
  `deploy/docker/docker-compose.yaml`.

Those components remain under their respective upstream licenses and are not
relicensed by AgentStrata. Container images are not embedded in this repository
or in the Python wheel. Operators and redistributors must review the license and
distribution terms for the exact versions they choose to install.

## Locked dependency evidence and frontend redistribution

[Dependency evidence](third_party/dependencies.json) inventories the direct and
transitive locked Python/JavaScript versions, scopes, hash-verified archives,
upstream declarations and hashes of inspected license files. The release-only
build closure is included. [Full original
notices](THIRD_PARTY_LICENSES.txt) preserve the package copyright/license texts,
including vendored-library notices carried by those packages. This is evidence
of upstream declarations, not an automatic opinion about every integration.
The inspected dependency archives are unmodified upstream distributions; scopes
and lockfile/package paths identify their usage. Repository adaptations are
listed separately above and below.
For npm runtime components, published version metadata supplies the upstream
`gitHead` where available; the inventory and notices link its immutable source
archive with build scripts, rather than relying solely on compiled npm output.
The exact version's registry metadata URL is retained for provenance.

Scopes distinguish Python runtime/extras and npm development/runtime-build
dependencies, including optional platform packages. The frontend emits these
texts at `third-party-notices.txt`; an inventory entry does not mean that its
component is present in the browser bundle or redistributed in the wheel.

For **elkjs 0.12.0**, upstream declares `EPL-2.0 OR GPL-3.0-or-later`. This AGPL
application uses its GPLv3 secondary-license route and AGPL section 13's
combination permission, retaining the EPL and secondary-license notices.
Preferred Java/GWT source and build scripts are in the
[elkjs 0.12.0 archive](https://github.com/kieler/elkjs/archive/refs/tags/0.12.0.tar.gz)
and [ELK 0.12.0 source](https://github.com/eclipse-elk/elk/tree/v0.12.0).
The compiled npm JavaScript archive alone is not the preferred Java source.
The registry-only binary wheels for pywin32 312 and sqlite-vec 0.1.9 also have
explicit preferred-source archives at their matching upstream tag commits in
the reviewed evidence and emitted notices.

Refresh with `.venv/bin/python scripts/third_party_notices.py --refresh`, review
the declarations and original texts, then run it without arguments for an offline
lock/evidence check. Refresh reads downloaded archives without installing or
executing their code. Missing or incompatible licensing requires a decision,
not an invented license or automatic component replacement.

## Independently licensed Open-LLM-VTuber integration

The [optional integration](integrations/open_llm_vtuber/README.md) does not vendor
the complete upstream applications, models or Live2D SDK. Its original Python
adapter is AGPL-3.0-only. Patches and upstream context have these exceptions:

- Backend revision `992309c0aa19845960228f880013d4685fde93b5` uses MIT. Preserve
  its [original license](third_party/Open-LLM-VTuber-LICENSE.txt) and copyright
  in the patched upstream application.
- Frontend revision `d176e7df2366952e3bacbf12cf9a8b18a4315932` uses
  [Open-LLM-VTuber License 1.0](third_party/Open-LLM-VTuber-Web-LICENSE.txt).
  `patches/frontend.patch`, including additions for that frontend, is supplied
  under those terms and excluded from the default AGPL grant. Paid hosting or
  access, commercial redistribution/rebranding and commercial embedding require
  the upstream commercial authorization specified there.
- Models, voices, avatars and Live2D SDK components retain their separate terms.

This remains an external option; its restrictions are not additional conditions
on the independent AgentStrata AGPL framework.

## Development and release tooling

Repository checks download the official Gitleaks 8.30.1 Linux archive at run
time and verify its pinned SHA-256 digest; Gitleaks is not redistributed by
AgentStrata. GitHub workflows reference official Actions by immutable commit
SHA. Those tools and Actions remain under their respective upstream licenses.

## Documentation adaptation

`CODE_OF_CONDUCT.md` is a condensed and modified adaptation of Contributor
Covenant 2.1, which is available under the Creative Commons Attribution 4.0
International license. The project version changes the scope, reporting, and
enforcement language. The upstream text and attribution are linked from the
adapted file.

The [CC BY 4.0 legal text](third_party/CC-BY-4.0-LICENSE.txt) is also supplied.
Project-generated evaluation illustrations and Console screenshots do not
redistribute their host's font software. Record source and authorization before
adding copied media.

## Separately obtained services

Compose pins SearXNG, Xiaohongshu MCP and Playwright MCP images by digest. Their
upstream programs use AGPL, MIT and Apache licenses respectively; image base
layers and bundled dependencies retain their own notices. These images are
pulled separately, not embedded in this repository, wheel or frontend. Obtain
the exact image's notices and corresponding source from its publisher before
redistributing it; this package inventory is not an image SBOM.

NapCat/QQ, remote GitHub MCP, external model APIs and operator-added services are
obtained independently. Their account, software, service and model terms remain
separate. The cc-connect package, automatic installers and built-in legacy Feishu launcher
have been removed; historical ACP protocol code does not bundle that software.

## Corresponding source

Keep the required licenses, copyright and modification notices in distributions.
Provide the exact covered version's corresponding source, including required
build/install scripts and non-system dependency source. The inventory records
pinned archive locations; redistributors remain responsible for continued access
and their modifications. The complete AgentStrata source is the versioned
repository archive; its Python sdist does not contain the Console source.

## Release maintenance

Before each public release:

1. inventory tracked binary and archive formats;
2. inspect newly copied or modified third-party source and media;
3. verify license and attribution requirements for packaged artifacts;
4. inspect dependency and container-image license changes; and
5. update this file when AgentStrata redistributes third-party material.

## Console arithmetic and declaration-only development exception

The original `@agentstrata/decimal-arithmetic` component is AGPL-3.0-only and
uses decimal.js (MIT) for the arithmetic API consumed by Arco Slider/Rate. The
npm override resolves Arco's old module name to this repository's own component;
no upstream number-precision source is copied. Vite/Rollup replaces the former
Rsbuild/Rspack chain, removing its WebAssembly helper dependency.

The development-only stackback 0.0.2 package declares MIT and names Roman
Shtylman as author, but its archive and fixed upstream commit provide no original
license file. The inventory marks this as **declaration-only evidence**, includes
the unmodified package declaration and standard MIT reference terms, and does
not invent a copyright year or claim an original notice was supplied. The
production build's `third-party-packages.json` allows checking its absence from
emitted browser modules. See [reviewed sources](third_party/license_sources.json).

# Plugin distribution notices

Copyright (C) 2026 Lingye. Original Python code is AGPL-3.0-only; the complete
license is supplied in LICENSE. These exceptions also apply inside wheel/sdist:

- `agentstrata_vtuber/resources/patches/backend.patch` contains MIT upstream
  context and changes for backend revision 992309c0aa19845960228f880013d4685fde93b5.
  Preserve Open-LLM-VTuber-LICENSE.txt in the separately installed application.
- `agentstrata_vtuber/resources/patches/frontend.patch`, including additions for
  that frontend, is under Open-LLM-VTuber License 1.0 and excluded from the AGPL
  grant. Its original terms are supplied in Open-LLM-VTuber-Web-LICENSE.txt.
  Target frontend revision: d176e7df2366952e3bacbf12cf9a8b18a4315932.
- Software, models, voices and Live2D SDK are downloaded separately and keep
  their original terms; none are embedded in this Python distribution.

The manifest and artifact lock preserve the exact upstream locations and
checksums. Corresponding source for this plugin is the matching complete
AgentStrata repository archive, including build/install scripts and patches.
The deployment's CHATCOPILOT_SOURCE_URL must identify the actual covered version;
downstream modifications need their own matching source. Credentials and user
data never belong in source archives.

Source: https://github.com/Ling-ye/AgentStrata

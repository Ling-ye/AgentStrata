# IFEval official implementation

Google Research, Apache-2.0. Revision: `26d8ccdab6fec61b5c83ad6327ea8bda9e580288`.
Source: https://github.com/google-research/google-research/tree/26d8ccdab6fec61b5c83ad6327ea8bda9e580288/instruction_following_eval

Modifications: namespace imports; private seeded language detector that raises scoring errors instead of accepting detection failure. Checkers otherwise unchanged. No upstream instruction is dropped. Sentence tokenizer resources must be prepared independently.

Original source SHA-256:

- instructions.py: `0165cde6379c3f6bba468a7759fa27247b72f4b8d5fd2821c27d72a34e0e25ce`
- instructions_util.py: `3c99e463ab63f73429efb75e333cbff6deb017e41658566e4c4bc4ecf39085db`
- instructions_registry.py: `042338c16bd33231b7b42612d1ca075f48f6fdbc53d0d5e7a700b29da416067b`

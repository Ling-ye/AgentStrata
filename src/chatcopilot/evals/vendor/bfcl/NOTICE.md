# BFCL official single-turn core

Upstream: UC Berkeley Gorilla / BFCL, Apache-2.0.
Revision: `f7cf7359b7ac615a0b294831c5ba2bc95ee4a000`.
Source: https://github.com/ShishirPatil/gorilla/tree/f7cf7359b7ac615a0b294831c5ba2bc95ee4a000/berkeley-function-call-leaderboard

The AST checker, language converters, enums and type mappings are copied unchanged except namespace imports. `schema.py` contains the upstream `_cast_to_openai_type` and `convert_to_tool`; `relevance.py` contains upstream `is_function_calling_format_output`, `is_empty_output` and `_evaluate_single_relevance_entry`. `language.py` contains the upstream language predicates and function-document preprocessing, including Java/JavaScript string representations. Extraction preserves function bodies. The handler type annotation is bound to `Any` to avoid importing upstream model handlers. The model registry is replaced with one explicit native OpenAI-compatible protocol profile (`underscore_to_dot=True`); this is not the tested model name. No model SDK, training, memory, search, CLI lifecycle or output writer is imported.

Original source checksums:

- `bfcl_eval/eval_checker/ast_eval/ast_checker.py`: `2aae7a68461a8f76c0be3894c8901b66b56967a1989d3ab066051e3fb97f1538`
- `bfcl_eval/constants/enums.py`: `2182becfa2a1d071ee1db30db593b4758c6bf866aa12d2d4b8daf09175ea518a`
- `bfcl_eval/constants/type_mappings.py`: `1702fb67afbe2c492608e58e2b7d02e46381f50166b47f3c952f76e34c7cd3bd`
- `bfcl_eval/eval_checker/ast_eval/type_convertor/java_type_converter.py`: `2fd4f4b0443b3dd974a1723bb4e45c086d7b352631062da7807ad1ad40706604`
- `bfcl_eval/eval_checker/ast_eval/type_convertor/js_type_converter.py`: `a114e9ff75c025cb52787ac33d6c2fbaa390905c6125a2b3c6afebab232bb5e4`
- `bfcl_eval/model_handler/utils.py`: `f78fd3edce603b333dc9a88ee2c041dc547d51f71aa449ffebc044c4b1e353f3`
- `bfcl_eval/eval_checker/eval_runner.py`: `b1033684908819ccb312d4d0e2c563359d69247412f00206013b2292b0e3ce81`
- `bfcl_eval/eval_checker/eval_runner_helper.py`: `7c756592af53eb54733989aa03a3fa87854f1c20dd566f3f10a770e17d25a9d7`
- `bfcl_eval/eval_checker/multi_turn_eval/multi_turn_utils.py`: `3ad32356ce0797cbf6fb906e4d21b2f61473384eec768ad55abebaeac846021e`
- `bfcl_eval/utils.py`: `3703b9bb63f83581c60b8e0b82aac74c360c9ca781e99e7d4eb28b0cce2285dd`

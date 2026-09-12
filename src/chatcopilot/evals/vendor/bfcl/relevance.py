from __future__ import annotations
from .enums import ReturnFormat
from typing import Any as BaseHandler

def is_function_calling_format_output(decoded_output):
    """
    Ensure the output is a list of dictionaries of the form:
    `[{func1: {param1: val1, param2: val2, ...}}, {func2: {param1: val1, param2: val2, ...}}, ...]`
    Sometimes the model handler's `decode_ast` method will return successfully, but the output is not in the correct format, and that will mess up the downstream evaluation that expects this format.
    This is especially the case when the model doesn't predict any function calls, and the output is an human-readable string.
    Note: Empty list `[]` is considered the correct format in this check.
    """
    if type(decoded_output) != list:
        return False
    for item in decoded_output:
        if type(item) != dict:
            return False
        # Check for `{func1: {param1: val1, param2: val2, ...}}`, should only have one key-value pair
        if len(item) != 1:
            return False
        # Check for `{param1: val1, param2: val2, ...}`; the parameter-value pairs should be a dictionary
        if type(list(item.values())[0]) != dict:
            return False
    return True

def is_empty_output(decoded_output):
    # This function is a patch to the ast decoder for relevance detection
    # Sometimes the ast decoder will parse successfully, but the input doens't really have a function call
    # [], [{}], and anything that is not in function calling format is considered empty (and thus should be marked as correct)
    if not is_function_calling_format_output(decoded_output):
        return True
    if len(decoded_output) == 0:
        return True
    if len(decoded_output) == 1 and len(decoded_output[0]) == 0:
        return True
    return False

def _evaluate_single_relevance_entry(
    handler: BaseHandler,
    index,
    model_result_item,
    prompt_entry,
    model_name,
    test_category,
):
    """Helper method to process a single relevance/irrelevance entry."""
    contain_func_call = False
    decoded_result = None
    decode_error = None

    try:
        decoded_result = handler.decode_ast(
            model_result_item, language=ReturnFormat.PYTHON, has_tool_call_tag=False
        )
        # Decode successfully, which means the model output is in valid function call format
        contain_func_call = True
        if is_empty_output(decoded_result):
            # Empty output is not considered as a valid function call
            contain_func_call = False
    except Exception as e:
        # Decode failed, which means the model output is not in valid function call format
        contain_func_call = False
        decode_error = str(e)

    # irrelevance test means no function call outputted
    if "irrelevance" in test_category:
        success = not contain_func_call
    else:
        success = contain_func_call

    if not success:
        temp = {
            "id": index,
            "model_name": model_name,
            "test_category": test_category,
            "valid": success,
            "prompt": prompt_entry,
            "model_result": model_result_item,
            "decoded_result": decoded_result,
        }
        if "irrelevance" in test_category:
            temp["error"] = ["Valid syntax. Successfully decode AST when it should not."]
            temp["error_type"] = "irrelevance_error:decoder_success"
        else:
            temp["error"] = [
                f"Invalid syntax. Failed to decode AST when it should have. {decode_error}"
            ]
            temp["error_type"] = "relevance_error:decoder_failed"
        return temp

    return {"valid": True}

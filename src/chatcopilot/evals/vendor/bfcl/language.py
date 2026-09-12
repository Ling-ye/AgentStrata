"""Upstream language-specific native FC schema preprocessing; see NOTICE.md."""
import json

def is_java(test_category):
    return "java" in test_category and not is_js(test_category)

def is_js(test_category):
    return "javascript" in test_category

def _get_language_specific_hint(test_category):
    if is_java(test_category):
        return " Note that the provided function is in Java 8 SDK syntax."
    elif is_js(test_category):
        return " Note that the provided function is in JavaScript syntax."
    else:
        return " Note that the provided function is in Python 3 syntax."

def _func_doc_language_specific_pre_processing(
    function: list[dict], test_category: str
) -> list[dict]:
    if len(function) == 0:
        return function

    assert type(function) == list
    for item in function:
        # Add language specific hints to the function description
        item["description"] = item["description"] + _get_language_specific_hint(
            test_category
        )
        # Process the parameters
        properties = item["parameters"]["properties"]
        if is_java(test_category):
            for key, value in properties.items():
                if value["type"] == "any":
                    properties[key][
                        "description"
                    ] += " This parameter can be of any type of Java object in string representation."
                else:
                    value[
                        "description"
                    ] += f" This is Java {value['type']} type parameter in string representation."
                if value["type"] == "ArrayList" or value["type"] == "Array":
                    value[
                        "description"
                    ] += f" The list elements are of type {value['items']['type']}; they are not in string representation."
                    del value["items"]

                value["type"] = "string"

        elif is_js(test_category):
            for key, value in properties.items():
                if value["type"] == "any":
                    properties[key][
                        "description"
                    ] += " This parameter can be of any type of JavaScript object in string representation."
                else:
                    value[
                        "description"
                    ] += f" This is JavaScript {value['type']} type parameter in string representation."
                if value["type"] == "array":
                    value[
                        "description"
                    ] += f" The list elements are of type {value['items']['type']}; they are not in string representation."
                    del value["items"]

                if value["type"] == "dict":
                    if "properties" in value:  # not every dict has properties
                        value[
                            "description"
                        ] += f" The dictionary entries have the following schema; they are not in string representation. {json.dumps(value['properties'])}"
                        del value["properties"]

                value["type"] = "string"

    return function

import keyword
import os
import re
from datetime import date, datetime
from inspect import getmodule
from pathlib import Path
from typing import Any, Dict, Set, Tuple, Type, Union, List

from uuid import UUID

import inflect
from pydantic import BaseModel, create_model
from pydantic.datetime_parse import StrBytesIntFloat, parse_date, parse_datetime

inflection_engine = inflect.engine()

TYPE_PREFERENCE_ORDER = [str,
                         bool,
                         float,
                         int,
                         date,
                         datetime]


def is_date(value: Union[date, StrBytesIntFloat]) -> bool:
    try:
        parse_date(value)
        return isinstance(value, (str, date))
    except:
        return False


def is_datetime(value: Union[datetime, StrBytesIntFloat]) -> bool:
    try:
        parse_datetime(value)
        return isinstance(value, (str, datetime))
    except:
        return False


def is_uuid(value) -> bool:
    if isinstance(value, UUID):
        return True
    try:
        if isinstance(value, str):
            UUID(value)
            return True
    except ValueError:
        pass
    return False


class ModelProperty(BaseModel):
    number_of_times_seen: int = 0
    observed_values: Set[Any] = set()
    type: Any
    is_list: bool = False


class Model(BaseModel):
    number_of_times_seen: int = 0
    keys: Dict[str, ModelProperty] = {}
    name: str


class JsonToModelParser(object):

    def __init__(self, root_name: str = "Root"):
        self.root_name: str = root_name
        self.models: Dict[str, Model] = {}

    def _get_type(self, name: str, value: Any) -> type:
        if isinstance(value, dict):
            _type = self.parse_dict(value, name=name)
        else:
            if str(value).replace(".", "", 1).replace("-", "", 1).isdigit():
                _type = type(value)
            elif is_datetime(value):
                _type = datetime
            elif is_date(value):
                _type = date
            elif is_uuid(value):
                _type = UUID
            else:
                _type = type(value)
        return _type

    def _get_dependencies_order(self, model: Model) -> List[str]:
        dependencies = []
        for key, property in model.keys.items():
            if isinstance(property.type, Model):
                dependencies.extend(self._get_dependencies_order(self.models.get(property.type.name)))
        dependencies.append(model.name)
        return dependencies

    def _normalize_key(self, key: str) -> str:
        return key.replace("_", " ").replace("-", " ").title().replace(" ", "")

    def parse_dict(self, model_dict: dict, name: str = None) -> Model:
        if name is None:
            name = self.root_name
        _model = self.models.get(name, Model(name=name))
        _model.number_of_times_seen += 1
        for key, value in model_dict.items():
            is_list = False
            if isinstance(value, list):
                is_list = True
                if inflection_engine.singular_noun(key):
                    singular_key = inflection_engine.singular_noun(key)
                else:
                    singular_key = key
                singular_key = self._normalize_key(singular_key)
                if len(value) > 0:
                    for _value in value:
                        _type = self._get_type(name + singular_key, _value)
                else:
                    _type = None
            else:
                _type = self._get_type(name + self._normalize_key(key), value)

            model_property = _model.keys.get(key, ModelProperty(type=_type))

            if value is not None:
                model_property.number_of_times_seen += 1
            model_property.is_list = is_list
            _model.keys[key] = model_property
            try:
                model_property.observed_values.add(value)
            except TypeError:
                # Error on non hashable types, we don't care
                pass

            if model_property.type is not _type and model_property.type != _type:
                print("Mismatch: Property '{name}' was type '{old_type}' but was now found to be '{new_type}'".format(name=key,
                                                                                                                      old_type=model_property.type,
                                                                                                                      new_type=_type))
                if model_property.type is None:
                    model_property.type = _type
                elif _type is not None:
                    if _type in TYPE_PREFERENCE_ORDER:
                        new_type_preference = TYPE_PREFERENCE_ORDER.index(_type)
                    else:
                        new_type_preference = len(TYPE_PREFERENCE_ORDER) + 1

                    if model_property.type in TYPE_PREFERENCE_ORDER:
                        old_type_preference = TYPE_PREFERENCE_ORDER.index(model_property.type)
                    else:
                        old_type_preference = len(TYPE_PREFERENCE_ORDER) + 1

                    model_property.type = _type if new_type_preference <= old_type_preference else model_property.type
                print("Resolved type to value '{new_type}'".format(new_type=model_property.type))

        self.models[name] = _model
        return _model

    def generate_models(self) -> Dict[str, Type[BaseModel]]:
        generated_models: Dict[Model, BaseModel] = {}
        for model_name in self._get_dependencies_order(self.models.get(self.root_name)):
            model = self.models.get(model_name)
            keys: Dict[str, Tuple[Any, Any]] = {}
            for key, model_property in model.keys.items():
                property_type = model_property.type

                if isinstance(property_type, Model):
                    property_type = generated_models.get(property_type.name)

                if model_property.is_list:
                    property_type = List[property_type]

                keys[key] = (property_type, ...)

            generated_models[model_name] = create_model(model_name, **keys)
        return generated_models

    def is_enum(self, property: ModelProperty) -> bool:
        if property.number_of_times_seen > 20 and property.type == str and float(len(property.observed_values)) / float(property.number_of_times_seen) < .5:
            return True
        return False

    def output_models_to_package(self, package: str, module_name: str) -> None:
        dependencies = {"from pydantic import BaseModel, Field", "from typing import Optional"}
        class_string = []
        for model_name in self._get_dependencies_order(self.models.get(self.root_name)):
            model = self.models.get(model_name)
            class_string.append("\n\nclass {model_name}(BaseModel):".format(model_name=model_name))
            for key, value in model.keys.items():
                if value.number_of_times_seen == model.number_of_times_seen:
                    optional = False
                else:
                    optional = True
                if isinstance(value.type, Model):
                    type = value.type.name
                else:
                    if value.type is None:
                        type = str("Any")
                        value.type = Any
                    else:
                        if self.is_enum(value):
                            print("{key} IS AN ENUM: {values}".format(key=key, values=value.observed_values))
                        type = str(value.type.__name__)
                    module = getmodule(value.type).__name__
                    if module != "builtins":
                        dependencies.add("from {module} import {type}".format(module=module, type=type))

                if value.is_list:
                    dependencies.add("from typing import List")
                    type = "List[{type}]".format(type=type)

                if optional:
                    type = "Optional[{type}]".format(type=type)

                if not key.isidentifier() or keyword.iskeyword(key):
                    cleaned_key = re.sub('\W|^(?=\d)', '_', key)
                    if keyword.iskeyword(cleaned_key):
                        cleaned_key = cleaned_key + "_"
                    default_value = "None" if optional else "..."
                    class_string.append("    {cleaned_key}: {type} = Field({default_value}, alias=\"{key}\")".format(key=key,
                                                                                                                     type=type,
                                                                                                                     default_value=default_value,
                                                                                                                     cleaned_key=cleaned_key))
                else:
                    class_string.append("    {key}: {type}".format(key=key, type=type))
        directory = os.path.join(*package.split("."))
        file = os.path.join(directory, "{module_name}.py".format(module_name=module_name))
        Path(directory).mkdir(parents=True, exist_ok=True)

        with open(file, "w") as file:
            file.write("\n".join(dependencies))
            file.write("\n")
            file.write("\n".join(class_string))
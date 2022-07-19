import os, os.path, re, logging, fnmatch, copy, time
from typing import Any, Tuple, List, Dict, Optional, Union
from dataclasses import dataclass
from omegaconf import MISSING, OmegaConf, DictConfig, ListConfig
from omegaconf.errors import OmegaConfBaseException
from collections import OrderedDict

from stimela.config import EmptyDictDefault, EmptyListDefault
import stimela
from stimela import log_exception, stimelogging
from stimela.exceptions import *
import scabha.exceptions
from scabha.exceptions import SubstitutionError, SubstitutionErrorList
from scabha.validate import evaluate_and_substitute, Unresolved, join_quote
from scabha.substitutions import SubstitutionNS, substitutions_from 
from .cab import Cab

Conditional = Optional[str]

@dataclass
class Step:
    """Represents one processing step of a recipe"""
    cab: Optional[str] = None                       # if not None, this step is a cab and this is the cab name
#    recipe: Optional["Recipe"] = None                    # if not None, this step is a nested recipe
    recipe: Optional[Any] = None                    # if not None, this step is a nested recipe
    params: Dict[str, Any] = EmptyDictDefault()     # assigns parameter values
    info: Optional[str] = None                      # comment or info
    skip: Optional[str] = None                      # if this evaluates to True, step is skipped 
    tags: List[str] = EmptyListDefault()
    backend: Optional["stimela.config.Backend"] = None                   # backend setting, overrides opts.config.backend if set

    name: str = ''                                  # step's internal name
    fqname: str = ''                                # fully-qualified name e.g. recipe_name.step_label

    assign: Dict[str, Any] = EmptyDictDefault()     # assigns recipe-level variables when step is executed

    assign_based_on: Dict[str, Any] = EmptyDictDefault()
                                                    # assigns recipe-level variables when step is executed based on value of another variable

    # runtime settings
    runtime: Dict[str, Any] = EmptyDictDefault()

    # _skip: Conditional = None                       # skip this step if conditional evaluates to true
    # _break_on: Conditional = None                   # break out (of parent recipe) if conditional evaluates to true

    def __post_init__(self):
        self.fqname = self.fqname or self.name
        if bool(self.cab) == bool(self.recipe):
            raise StepValidationError("step '{self.name}': step must specify either a cab or a nested recipe, but not both")
        self.cargo = self.config = None
        self.tags = set(self.tags)
        # convert params into standard dict, else lousy stuff happens when we insert non-standard objects
        if isinstance(self.params, DictConfig):
            self.params = OmegaConf.to_container(self.params)
        # after (pre)validation, this contains parameter values
        self.validated_params = None
        # the "skip" attribute is reevaluated at runtime since it may contain substitutions, but if it's set to a bool
        # constant, self._skip will be preset already
        if self.skip in {"True", "true", "1"}:
            self._skip = True
        elif self.skip in {"False", "false", "0", "", None}:
            self._skip = False
        else:
            # otherwise, self._skip stays at None, and will be re-evaluated at runtime
            self._skip = None
        
    def summary(self, params=None, recursive=True, ignore_missing=False):
        return self.cargo and self.cargo.summary(recursive=recursive, 
                                params=params or self.validated_params or self.params, ignore_missing=ignore_missing)

    @property
    def finalized(self):
        return self.cargo is not None

    @property
    def missing_params(self):
        return OrderedDict([(name, schema) for name, schema in self.cargo.inputs_outputs.items() 
                            if schema.required and name not in self.validated_params])

    @property
    def invalid_params(self):
        return [name for name, value in self.validated_params.items() if isinstance(value, scabha.exceptions.Error)]

    @property
    def unresolved_params(self):
        return [name for name, value in self.validated_params.items() if isinstance(value, Unresolved)]

    @property
    def inputs(self):
        return self.cargo.inputs

    @property
    def outputs(self):
        return self.cargo.outputs

    @property
    def inputs_outputs(self):
        return self.cargo.inputs_outputs

    @property
    def log(self):
        """Logger object passed from cargo"""
        return self.cargo and self.cargo.log
    
    @property
    def logopts(self):
        """Logger options passed from cargo"""
        return self.cargo and self.cargo.logopts

    @property
    def nesting(self):
        """Logger object passed from cargo"""
        return self.cargo and self.cargo.nesting

    def update_parameter(self, name, value):
        self.params[name] = value

    _instantiated_cabs = {}

    def finalize(self, config=None, log=None, logopts=None, fqname=None, nesting=0):
        from .recipe import Recipe, RecipeSchema
        if not self.finalized:
            if fqname is not None:
                self.fqname = fqname
            self.config = config = config or stimela.CONFIG

            # if recipe, validate the recipe with our parameters
            if self.recipe:
                # first, if it is a string, look it up in library
                recipe_name = "nested recipe"
                if type(self.recipe) is str:
                    recipe_name = f"nested recipe '{self.recipe}'"
                    # undotted name -- look in lib.recipes
                    if '.' not in self.recipe:
                        if self.recipe not in self.config.lib.recipes:
                            raise StepValidationError(f"step '{self.name}': '{self.recipe}' not found in lib.recipes")
                        self.recipe = self.config.lib.recipes[self.recipe]
                    # dotted name -- look in config
                    else: 
                        section, var = resolve_dotted_reference(self.recipe, config, current=None, context=f"step '{self.name}'")
                        if var not in section:
                            raise StepValidationError(f"step '{self.name}': '{self.recipe}' not found")
                        self.recipe = section[var]
                    # self.recipe is now hopefully a DictConfig or a Recipe object, so fall through below to validate it 
                # instantiate from omegaconf object, if needed
                if type(self.recipe) is DictConfig:
                    try:
                        self.recipe = Recipe(**OmegaConf.unsafe_merge(RecipeSchema.copy(), self.recipe))
                    except OmegaConfBaseException as exc:
                        raise StepValidationError(f"step '{self.name}': error in recipe '{recipe_name}", exc)
                elif type(self.recipe) is not Recipe:
                    raise StepValidationError(f"step '{self.name}': recipe field must be a string or a nested recipe, found {type(self.recipe)}")
                self.cargo = self.recipe
            else:
                if self.cab in self._instantiated_cabs:
                    self.cargo = copy.copy(self._instantiated_cabs[self.cab])
                else:
                    if self.cab not in self.config.cabs:
                        raise StepValidationError(f"step '{self.name}': unknown cab {self.cab}")
                    try:
                        self.cargo = self._instantiated_cabs[self.cab] = Cab(**config.cabs[self.cab])
                    except Exception as exc:
                        raise StepValidationError(f"step '{self.name}': error in cab '{self.cab}'", exc)
            self.cargo.name = self.name

            # flatten parameters
            self.params = self.cargo.flatten_param_dict(OrderedDict(), self.params)

            # if logger is not provided, then init one
            if log is None:
                log = stimela.logger().getChild(self.fqname)
                log.propagate = True

            # finalize the cargo
            self.cargo.finalize(config, log=log, logopts=logopts, fqname=self.fqname, nesting=nesting)

            # build dictionary of defaults from cargo
            self.defaults = {name: schema.default for name, schema in self.cargo.inputs_outputs.items() 
                             if schema.default is not None and not isinstance(schema.default, Unresolved) }
            self.defaults.update(**self.cargo.defaults)
            
            # set missing parameters from defaults
            for name, value in self.defaults.items():
                if name not in self.params:
                    self.params[name] = value



    def prevalidate(self, subst: Optional[SubstitutionNS]=None, root=False):
        self.finalize()
        # validate cab or recipe
        params = self.validated_params = self.cargo.prevalidate(self.params, subst, root=root)
        self.log.debug(f"{self.cargo.name}: {len(self.missing_params)} missing, "
                        f"{len(self.invalid_params)} invalid and "
                        f"{len(self.unresolved_params)} unresolved parameters")
        if self.invalid_params:
            raise StepValidationError(f"step '{self.name}': {self.cargo.name} has the following invalid parameters: {join_quote(self.invalid_params)}")
        return params

    def log_summary(self, level, title, color=None, ignore_missing=True):
        extra = dict(color=color, boldface=True)
        if self.log.isEnabledFor(level):
            self.log.log(level, f"### {title}", extra=extra)
            del extra['boldface']
            for line in self.summary(recursive=False, ignore_missing=ignore_missing):
                self.log.log(level, line, extra=extra)

    def run(self, subst=None, batch=None, parent_log=None):
        """Runs the step"""
        from .recipe import Recipe
        from . import runners

        if self.validated_params is None:
            self.prevalidate(self.params)
        # some messages go to the parent logger -- if not defined, default to our own logger
        if parent_log is None:
            parent_log = self.log

        with stimelogging.declare_subtask(self.name) as subtask:
            # evaluate the skip attribute (it can be a formula and/or a {}-substititon)
            skip = self._skip
            if self._skip is None and subst is not None:
                skips = dict(skip=self.skip)
                skips = evaluate_and_substitute(skips, subst, subst.current, location=[self.fqname], ignore_subst_errors=False)
                skip = skips["skip"]

            # Since prevalidation will have populated default values for potentially missing parameters, use those values
            # For parameters that aren't missing, use whatever value that was suplied
            params = self.validated_params.copy()
            params.update(**self.params)

            skip_warned = False   # becomes True when warnings are given

            self.log.debug(f"validating inputs {subst and list(subst.keys())}")
            validated = None
            try:
                params = self.cargo.validate_inputs(params, loosely=skip, subst=subst)
                validated = True

            except ScabhaBaseException as exc:
                level = logging.WARNING if skip else logging.ERROR
                if not exc.logged:
                    if type(exc) is SubstitutionErrorList:
                        self.log.log(level, f"unresolved {{}}-substitution(s):")
                        for err in exc.errors:
                            self.log.log(level, f"  {err}")
                    else:
                        self.log.log(level, f"error validating inputs: {exc}")
                    exc.logged = True
                self.log_summary(level, "summary of inputs follows", color="WARNING")
                # raise up, unless step is being skipped
                if self.skip:
                    self.log.warning("since the step is being skipped, this is not fatal")
                    skip_warned = True
                else:
                    raise

            self.validated_params.update(**params)

            # log inputs
            if validated and not skip:
                self.log_summary(logging.INFO, "validated inputs", color="GREEN", ignore_missing=True)
                if subst is not None:
                    subst.current = params

            # bomb out if some inputs failed to validate or substitutions resolve
            if self.invalid_params or self.unresolved_params:
                invalid = self.invalid_params + self.unresolved_params
                if self.skip:
                    self.log.warning(f"invalid inputs: {join_quote(invalid)}")
                    if not skip_warned:
                        self.log.warning("since the step was skipped, this is not fatal")
                        skip_warned = True
                else:
                    raise StepValidationError(f"step '{self.name}': invalid inputs: {join_quote(invalid)}", log=self.log)

            if not skip:
                if type(self.cargo) is Recipe:
                    self.cargo._run(params)
                elif type(self.cargo) is Cab:
                    if self.backend is not None:
                        backend = self.backend
                    elif self.cargo.backend is not None:
                        backend = self.cargo.backend
                    else:
                        backend =  stimela.CONFIG.opts.backend
                    runners.run_cab(self, params, backend=backend, subst=subst, batch=batch)
                else:
                    raise RuntimeError("step '{self.name}': unknown cargo type")
            else:
                if self._skip is None and subst is not None:
                    self.log.info(f"skipping step based on setting of '{self.skip}'")
                else:
                    self.log.info("skipping step based on explicit setting")

            self.log.debug(f"validating outputs")
            validated = False

            try:
                params = self.cargo.validate_outputs(params, loosely=skip, subst=subst)
                validated = True
            except ScabhaBaseException as exc:
                level = logging.WARNING if self.skip else logging.ERROR
                if not exc.logged:
                    if type(exc) is SubstitutionErrorList:
                        self.log.log(level, f"unresolved {{}}-substitution(s):")
                        for err in exc.errors:
                            self.log.log(level, f"  {err}")
                    else:
                        self.log.log(level, f"error validating outputs: {exc}")
                    exc.logged = True
                # raise up, unless step is being skipped
                if skip:
                    self.log.warning("since the step was skipped, this is not fatal")
                else:
                    self.log_summary(level, "failed outputs", color="WARNING")
                    raise

            if validated:
                self.validated_params.update(**params)
                if subst is not None:
                    subst.current._merge_(params)
                self.log_summary(logging.DEBUG, "validated outputs", ignore_missing=True)

            # bomb out if an output was invalid
            invalid = [name for name in self.invalid_params + self.unresolved_params if name in self.cargo.outputs]
            if invalid:
                if skip:
                    self.log.warning(f"invalid outputs: {join_quote(invalid)}")
                    self.log.warning("since the step was skipped, this is not fatal")
                else:
                    raise StepValidationError(f"invalid outputs: {join_quote(invalid)}", log=self.log)

        return params

@dataclass
class ForLoopClause(object):
    # name of list variable
    var: str 
    # This should be the name of an input that provides a list, or a list
    over: Any
    # If True, this is a scatter not a loop -- things may be evaluated in parallel
    scatter: bool = False


def resolve_dotted_reference(key, base, current, context): 
    """helper function to look up a key like a.b.c in a nested dict-like structure"""
    path = key.split('.')
    if path[0]:
        section = base
    else:
        if not current:
            raise NameError(f"{context}: leading '.' not permitted here")
        section = current
        path = path[1:]
        if not path:
            raise NameError(f"{context}: '.' not permitted")
    varname = path[-1]
    for element in path[:-1]:
        if not element:
            raise NameError(f"{context}: '..' not permitted")
        if element in section:
            section = section[element]
        else:
            raise NameError(f"{context}: '{element}' in '{key}' is not a valid config section")
    return section, varname

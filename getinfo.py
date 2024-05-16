#################################################################################
# WaterTAP Copyright (c) 2020-2024, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory, Oak Ridge National Laboratory,
# National Renewable Energy Laboratory, and National Energy Technology
# Laboratory (subject to receipt of any required approvals from the U.S. Dept.
# of Energy). All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# information, respectively. These files are also available online at the URL
# "https://github.com/watertap-org/getinfo/"
#################################################################################
import argparse
import collections
import datetime
import json
import fnmatch
import functools
import importlib.metadata
import inspect
import locale
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    NamedTuple,
    Union,
)
from types import (
    FunctionType,
    MethodType,
    ModuleType,
)


__version__ = "v0.24.5.15.2"

_log = logging.getLogger("getinfo")


class _Node:
    def __init__(self, parent=None, id=None):
        self._parent = parent
        self._id = id

    @classmethod
    def from_parent(cls, parent: "Collector", *args, **kwargs):
        return cls(
            *args, parent=parent, **kwargs,
        )


class Result(_Node):
    def __init__(self, value=None, error=None, **kwargs):
        super().__init__(**kwargs)
        self.value = value
        self.error = error


class Provider(_Node):

    known_type: type = None

    def __call__(self) -> object: pass
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({self._id!r}, {self.known_type})>"


class Collector(_Node):
    def __init__(self, *, target: str = None, **kwargs):
        super().__init__(**kwargs)
        self._target = target 

    def collect(self) -> Iterable[Provider]:
        for name, attr in self.__dict__.items():
            if name.startswith("_"): continue
            yield InstanceAttribute.from_parent(self, name=name, obj=self)

        for name, cls_attr in type(self).__dict__.items():
            if name.startswith("_"): continue
            yield InstanceMethod.from_parent(self, name=name, obj=self)


class InstanceAttribute(Provider):
    def __init__(self, name: str, obj: object, **kwargs):
        super().__init__(id=name, **kwargs)
        v = self._value = getattr(obj, name)
        self.known_type = type(v)

    def __call__(self):
        return self._value


class InstanceMethod(Provider):
    def __init__(self, name: str, obj: object, **kwargs):
        super().__init__(id=name, **kwargs)
        self._meth = getattr(obj, name, None)
        if not inspect.ismethod(self._meth): raise TypeError(self._meth)
        self._type = type(obj)
        self.known_type = inspect.signature(self._meth).return_annotation

    def __call__(self):
        return get_collectable(self._meth(), parent=self)


class Value(Provider):
    def __init__(self, value: Any, id: str = None, parent: _Node = None, **kwargs):
        if id is None:
            id = parent._id
        super().__init__(parent=parent, id=id, **kwargs)
        self.known_type = type(value)
        self._value = value

    def __call__(self):
        return self._value

    def __repr__(self):
        return f"<{self.__class__.__name__}({self._value}, id={self._id})>"


def _get_full_name(obj: Any) -> str:
    parts = []
    if module_name := getattr(obj, "__module__", None):
        parts.append(module_name)
    if own_name := getattr(obj, "__name__", None):
        parts.append(own_name)
    return ".".join(parts)


class Function(Provider):
    def __init__(self, func: FunctionType, id: str = None, **kwargs):
        self._func_full_name = _get_full_name(func)
        if id is None:
            id = f"{self._func_full_name}()"
        super().__init__(id=id, **kwargs)
        self._func = func
        self.known_type = inspect.signature(self._func).return_annotation

    def __call__(self):
        return self._func()


def _get_providers(obj: object):
    by_name = {}
    for name, attr in obj.__dict__.items():
        if name.startswith("_"): continue
        by_name[name] = InstanceAttribute(name=name, obj=obj)

    for name, cls_attr in type(obj).__dict__.items():
        if name.startswith("_"): continue
        meth = getattr(obj, name, None)
        if not isinstance(meth, MethodType): continue
        by_name[name] = Routine(
            func=meth,
            name=name,
        )
    return by_name


class FSPath(Collector):
    def __init__(self, src: Union[str, Path], **kwargs):
        super().__init__(target=src, **kwargs)
        self.path = Path(src)
        self.resolved = self.path.resolve()

    def _stat(self):
        return self.path.stat()

    def _get_stat_timestamp(self, name: str):
        ts = getattr(self._stat(), f"st_{name}")
        return datetime.datetime.fromtimestamp(ts)


class File(FSPath):

    def ctime(self) -> datetime.datetime:
        return self._get_stat_timestamp("ctime")

    def mtime(self) -> datetime.datetime:
        return self._get_stat_timestamp("mtime")

    def size(self) -> int:
        return self._stat().st_size


class Directory(FSPath):

    def ctime(self) -> datetime.datetime:
        return self._get_stat_timestamp("ctime")

    def mtime(self) -> datetime.datetime:
        return self._get_stat_timestamp("mtime")

    def contents_by_suffix(self) -> dict:
        suffixes = (
            p.suffix
            for p in self.path.rglob('*')
        )
        return dict(
            collections.Counter(suffixes)
        )



class Executable(Collector):
    def __init__(self, invoked_as: str, **kwargs):
        super().__init__(target=invoked_as, **kwargs)
        self.invoked_as = invoked_as

    def file(self) -> FSPath:
        path = shutil.which(self.invoked_as)
        if path is None:
            return
        return File(path)


class DictCollector(Collector):
    def __init__(self, data: dict, **kwargs):
        super().__init__(**kwargs)
        self._data = data

    def collect(self):
        pass


def _matches_any(s: str, patterns: list) -> bool:
    return any(
        fnmatch.fnmatch(s, pat)
        for pat in patterns
    )


class Environ(Provider):
    def __init__(self, *patterns: str, **kwargs):
        self._patterns = list(patterns)
        self._patterns_repr = self.name = "|".join(self._patterns)
        super().__init__(id=f"os.environ[{self._patterns_repr}]", **kwargs)

    def __call__(self) -> dict:
        return {
            var: val
            for var, val in os.environ.items()
            if _matches_any(var, self._patterns)
        }


class ActiveCondaEnv(Collector):
    def name(self) -> str:
        return os.environ.get("CONDA_DEFAULT_ENV", "")

    def python(self) -> Executable:
        return Executable("python")

    def conda(self) -> Executable:
        return Executable("conda")


@functools.singledispatch
def populate(obj: object, data: dict = None, parent=None):
    key = get_key(parent)
    data[key] = obj


@populate.register
def _for_collector(obj: Collector, data: dict=None, parent=None):
    if data is None:
        # we expect data to be None only for (root) collectors
        data = {}
    key = get_key(obj)
    subdata = data[key] = {}
    _log.info("Collecting from: %s", key)
    for collected in obj.collect():
        populate(collected, data=subdata, parent=parent)


@populate.register
def _for_provider(obj: Provider, data: dict, parent=None):
    key = get_key(obj)
    try:
        res = obj()
    except Exception as e:
        data[key] = {"error": e}
        return
    populate(res, data=data, parent=obj)


def get_key(obj: object) -> str:

    if (id_ := getattr(obj, "_id", None)) is not None:
        return id_
    return get_default_id(obj)


@functools.singledispatch
def get_default_id(obj: object) -> str:
    raise TypeError(f"{obj}, {type(obj)=}")


@get_default_id.register
def _for_provider(obj: Provider) -> str:
    return type(obj).__name__.lower()


@get_default_id.register
def _for_collector(c: Collector) -> str:
    s = type(c).__name__.lower()
    if c._target:
        s += f"[{c._target}]"
    return s


class ModuleFunction(Collector):
    def __init__(self, func, prefix: str = "", **kwargs):
        super().__init__(**kwargs)
        self._func = func

    def collect(self):
        res = self._func()
        items = res if inspect.isgenerator(res) else [res]
        for item in items:
            collectable_item = get_collectable(item, parent=self)
            yield collectable_item


@functools.singledispatch
def get_collectable(obj: object, parent=None, id=None):
    if isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str):
        id, obj = obj

    if isinstance(obj, _Node):
        if obj._id is None:
            obj._id = id
        if obj._parent is None:
            obj._parent = parent
        if obj._id is None:
            obj._id = obj._parent._id
        return obj

    if hasattr(obj, "__name__") and callable(obj):
        return Function.from_parent(parent, func=obj, id=id)

    return Value.from_parent(parent, value=obj, id=id)


# @get_collectable.register(Collector)
# @get_collectable.register(Provider)
# def _as_is(obj, id=None, **kwargs):
#     return obj


class Module(Collector):
    def __init__(self, module: ModuleType = None, prefix: str = "", **kwargs):
        super().__init__(**kwargs)
        self._module = module
        self._prefix = prefix

    def collect(self):
        vars_ = dict(globals() if self._module is None else self._module.__dict__)
        for name, attr in vars_.items():
            if not name.startswith(self._prefix): continue
            if inspect.isfunction(attr):
                # yield Function.from_parent(self, attr, name=name.replace(self._prefix, ""))
                yield ModuleFunction.from_parent(self, attr, target=name.replace(self._prefix, ""))
            elif isinstance(attr, type):
                raise NotImplementedError
            else:
                yield Value.from_parent(self, value=attr, id=name)


def getinfo_meta():
    yield "version", __version__
    yield "__file__", File(__file__)


def getinfo_conda():
    yield ActiveCondaEnv()
    yield Environ("CONDA_*")


def getinfo_python():
    yield "sys.executable", sys.executable
    yield "which python", Executable("python")
    yield "sys.version_info", sys.version_info
    yield PipList()


class PythonDistribution(Collector):
    def __init__(self, name: str, **kwargs):
        super().__init__(target=name, **kwargs)
        self._name = name

    @functools.cached_property
    def _dist(self) -> importlib.metadata.Distribution:
        return importlib.metadata.distribution(self._name)

    def version(self) -> str:
        return self._dist.version

    def requires(self) -> list:
        return self._dist.requires


class PythonPackage(Collector):
    def __init__(self, name: str, **kwargs):
        super().__init__(target=name, **kwargs)
        self._name = name
        self._spec = importlib.util.find_spec(self._name)

    def origin(self):
        if self._spec is None: return
        if origin := self._spec.origin:
            return File(origin)

    def directory(self):
        if self._spec is None: return
        if locs := self._spec.submodule_search_locations:
            return Directory(locs[0])


class RunShell(Collector):
    def __init__(self, cmd: str, **kwargs):
        super().__init__(target=cmd, **kwargs)
        self.command = self._cmd = cmd

    @functools.cached_property
    def _result(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._cmd,
            text=True,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def exit_code(self) -> int:
        return self._result.returncode

    def stdout_lines(self) -> list:
        return (
            self._result
            .stdout
            .strip()
            .splitlines()
        )

    def stderr_lines(self) -> list:
        return (
            self._result
            .stderr
            .strip()
            .splitlines()
        )


class RunPy(Collector):
    def __init__(self, code: Union[str, list], python_exe: str = "python", **kwargs):
        if isinstance(code, list):
            code = "; ".join(code)
        super().__init__(target=code, **kwargs)
        self.code = self._code = code
        self.python_exe = self._python_exe = python_exe

    @functools.cached_property
    def _result(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._python_exe, "-c", self._code],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def exit_code(self) -> int:
        return self._result.returncode

    def output(self) -> str:
        return (
            self._result
            .stdout
            .strip()
        )


class Solver(Collector):
    def __init__(self,
            name: str,
            **kwargs,
        ):
        target = name
        super().__init__(target=target, **kwargs)
        self.name = self._name = name

        self._code = [
            "from pyomo.environ import SolverFactory",
            f"sf = SolverFactory('{self._name}')",
            "sf.available(exception_flag=False)",
            "print(sf.executable())",
        ]
        self._runner = RunPy(
            code=self._code
        )

    @functools.cached_property
    def _output(self):
        return self._runner.output()

    @functools.cached_property
    def _found(self) -> bool:
        return "could not locate" not in self._output.lower()

    def probe_code(self):
        return self._runner.code

    def output(self):
        return self._output

    def executable(self) -> str:
        if not self._found: return
        return Executable(self._output)


class IDAESSolver(Collector):
    def __init__(self,
            name: str,
            **kwargs,
        ):
        target = name
        super().__init__(target=target, **kwargs)
        self.name = self._name = name
        self._code = [
            "from idaes.core.solvers import get_solver",
            f"s = get_solver('{self._name}')",
            "print(s.executable())",
        ]

        self._runner = RunPy(code=self._code)

    @functools.cached_property
    def _output(self):
        return self._runner.output()

    @functools.cached_property
    def _found(self) -> bool:
        return self._runner.exit_code() == 0

    def probe_code(self):
        return self._runner.code

    def output(self):
        return self._output

    def executable(self) -> str:
        if not self._found: return
        return Executable(self._output)


class DependenciesOf(Collector):
    def __init__(self, name: str, **kwargs):
        super().__init__(target=name, **kwargs)
        self._name = name

    def _requires(self, dist: importlib.metadata.Distribution) -> bool:
        requirements = dist.requires or []
        for req in requirements:
            if self._name in req.lower():
                return True
        return False

    def collect(self):
        for dist in importlib.metadata.distributions():
            if self._requires(dist):
                yield PythonDistribution(name=dist.metadata["Name"])


class PipList(Provider):

    def __call__(self) -> dict:
        cmd_output = (
            subprocess.check_output(
                ["pip", "list"],
                text=True,
            )
            .strip()
        )
        res = {}
        for line in cmd_output.splitlines():
            if line.startswith("Package") or line.startswith("---"): continue
            parts = line.split(maxsplit=2)
            if len(parts) == 2:
                name, version = parts
                res[name] = version
            else:
                name, rest = parts[0], parts[1:]
                res[name] = rest
        return res


def getinfo_working_dir():
    yield os.getcwd
    yield "os.getcwd()", os.getcwd
    yield Directory(".")


def getinfo_idaes():
    yield PythonDistribution("pyomo")
    yield PythonDistribution("idaes-pse")
    yield PythonPackage("idaes")
    yield RunPy("import idaes; print(idaes.__path__[0])")
    yield RunShell("pip show idaes-pse")
    yield RunShell("idaes bin-directory")
    yield DependenciesOf("idaes-pse")
    yield DependenciesOf("pyomo")
    yield DependenciesOf("pandas")


def getinfo_solvers():
    for name in [
        "ipopt",
        "cbc",
    ]:
        yield Solver(name)
        yield IDAESSolver(name)


def getinfo_watertap():
    yield PythonDistribution("watertap")
    yield PythonPackage("watertap")
    yield RunShell("pip show watertap")


def getinfo_watertap_ui():
    yield PythonDistribution("watertap-ui")
    yield RunShell("pip show watertap-ui")


def getinfo_platform():
    yield from (
        locale.getlocale,
        platform.platform,
        platform.system,
        platform.machine,
        platform.processor,
        platform.version,
        platform.release,
        platform.mac_ver,
        platform.win32_ver,
        platform.win32_edition,
    )



def _to_jsonable(obj: object):
    if isinstance(obj, Exception):
        return repr(obj)
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, os.PathLike):
        return os.fspath(obj)
    if isinstance(obj, NamedTuple):
        return obj._asdict()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


def _get_default_file_name(prefix: str = ".getinfo-output") -> str:

    try:
        # Python 3.11+
        ts = datetime.datetime.now(tz=datetime.UTC)
    except AttributeError:
        ts = datetime.datetime.now(tz=datetime.timezone.utc)
    name = f"{prefix}-{ts.isoformat()}"
    return (
        name
        # : are used in isoformat() but they are not allowed in file names on Windows
        .replace(":", "-")
    )


# created in the global scope to facilitate debugging
DATA = {}


def main(args=None):

    parser = _build_cli_parser()
    cli_opts = parser.parse_args(args)
    logging.basicConfig(level=logging.INFO)
    _log.info(parser.prog)
    _log.debug(cli_opts)

    _log.info("Start collecting information")
    root = Module(target=__name__, prefix="getinfo_")
    populate(root, DATA)
    _log.info("Collection complete")

    output_str = json.dumps(DATA, indent=4, default=_to_jsonable)
    n_lines = output_str.count("\n")
    output_dest = cli_opts.output
    if output_dest in {"stdout"}:
        _log.info("output (%d lines) will be written to stdout", n_lines)
        print(output_str)
    else:
        path = Path(output_dest) if output_dest else Path(_get_default_file_name()).with_suffix(".json")
        path = path.resolve()
        written = path.write_text(output_str)
        _log.info("output (%d lines) has been written to %s (%d B)", n_lines, os.fspath(path), written)


def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"getinfo {__version__}")
    p.add_argument(
        "-o",
        "--output",
        dest="output",
        help="""
        Where the output will be written to. Can be a path to a file or `stdout`.
        If this flag is not specified (default), an autogenerated filename will be used.
        """,
        default="",
    )
    p.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    return p


if __name__ == '__main__':
    main()

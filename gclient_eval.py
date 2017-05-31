# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import ast
import copy
import functools

from third_party import schema


# See https://github.com/keleshev/schema for docs how to configure schema.
_GCLIENT_HOOKS_SCHEMA = [{
    # Hook action: list of command-line arguments to invoke.
    'action': [basestring],

    # Name of the hook. Doesn't affect operation.
    schema.Optional('name'): basestring,

    # Hook pattern (regex). Originally intended to limit some hooks to run
    # only when files matching the pattern have changed. In practice, with git,
    # gclient runs all the hooks regardless of this field.
    schema.Optional('pattern'): basestring,
}]

_GCLIENT_SCHEMA = schema.Schema({
    # List of host names from which dependencies are allowed (whitelist).
    # NOTE: when not present, all hosts are allowed.
    # NOTE: scoped to current DEPS file, not recursive.
    schema.Optional('allowed_hosts'): [schema.Optional(basestring)],

    # Mapping from paths to repo and revision to check out under that path.
    # Applying this mapping to the on-disk checkout is the main purpose
    # of gclient, and also why the config file is called DEPS.
    #
    # The following functions are allowed:
    #
    #   Var(): allows variable substitution (either from 'vars' dict below,
    #          or command-line override)
    schema.Optional('deps'): {
        schema.Optional(basestring): schema.Or(
            basestring,
            {
                'url': basestring,
            },
        ),
    },

    # Similar to 'deps' (see above) - also keyed by OS (e.g. 'linux').
    # Also see 'target_os'.
    schema.Optional('deps_os'): {
        schema.Optional(basestring): {
            schema.Optional(basestring): schema.Or(basestring, None)
        }
    },

    # Hooks executed after gclient sync (unless suppressed), or explicitly
    # on gclient hooks. See _GCLIENT_HOOKS_SCHEMA for details.
    # Also see 'pre_deps_hooks'.
    schema.Optional('hooks'): _GCLIENT_HOOKS_SCHEMA,

    # Similar to 'hooks', also keyed by OS.
    schema.Optional('hooks_os'): {
        schema.Optional(basestring): _GCLIENT_HOOKS_SCHEMA
    },

    # Rules which #includes are allowed in the directory.
    # Also see 'skip_child_includes' and 'specific_include_rules'.
    schema.Optional('include_rules'): [schema.Optional(basestring)],

    # Hooks executed before processing DEPS. See 'hooks' for more details.
    schema.Optional('pre_deps_hooks'): _GCLIENT_HOOKS_SCHEMA,

    # Whitelists deps for which recursion should be enabled.
    schema.Optional('recursedeps'): [
        schema.Optional(schema.Or(
            basestring,
            (basestring, basestring),
            [basestring, basestring]
        )),
    ],

    # Blacklists directories for checking 'include_rules'.
    schema.Optional('skip_child_includes'): [schema.Optional(basestring)],

    # Mapping from paths to include rules specific for that path.
    # See 'include_rules' for more details.
    schema.Optional('specific_include_rules'): {
        schema.Optional(basestring): [basestring]
    },

    # List of additional OS names to consider when selecting dependencies
    # from deps_os.
    schema.Optional('target_os'): [schema.Optional(basestring)],

    # For recursed-upon sub-dependencies, check out their own dependencies
    # relative to the paren't path, rather than relative to the .gclient file.
    schema.Optional('use_relative_paths'): bool,

    # Variables that can be referenced using Var() - see 'deps'.
    schema.Optional('vars'): {
        schema.Optional(basestring): schema.Or(basestring, bool)
    },
})


def _gclient_eval(
    node_or_string, global_scope, filename='<unknown>', expose_vars=False):
  """Safely evaluates a single expression. Returns the result."""
  _allowed_names = {'None': None, 'True': True, 'False': False}
  if isinstance(node_or_string, basestring):
    node_or_string = ast.parse(node_or_string, filename=filename, mode='eval')
  if isinstance(node_or_string, ast.Expression):
    node_or_string = node_or_string.body
  def _convert(node, vars_dict=None):
    if not vars_dict:
      vars_dict = {}
    vars_dict = copy.deepcopy(vars_dict)
    vars_dict.update(_allowed_names)
    vars_convert = functools.partial(_convert, vars_dict=vars_dict)
    if isinstance(node, ast.Str):
      return node.s
    elif isinstance(node, ast.Tuple):
      return tuple(map(vars_convert, node.elts))
    elif isinstance(node, ast.List):
      return list(map(vars_convert, node.elts))
    elif isinstance(node, ast.Dict):
      result = {}
      for k, v in zip(node.keys, node.values):
        c_k = _convert(k, vars_dict=vars_dict)
        c_v = _convert(v, vars_dict=vars_dict)
        if expose_vars:
          vars_dict[c_k] = c_v
        result[c_k] = c_v
      return result
    elif isinstance(node, ast.Name):
      if node.id not in vars_dict:
        raise ValueError(
            'invalid name %r; available names: %r (file %r, line %s)' % (
                node.id, vars_dict, filename,
                getattr(node, 'lineno', '<unknown>')))
      return vars_dict[node.id]
    elif isinstance(node, ast.Call):
      if not isinstance(node.func, ast.Name):
        raise ValueError(
            'invalid call: func should be a name (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      if node.keywords or node.starargs or node.kwargs:
        raise ValueError(
            'invalid call: use only regular args (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      args = map(vars_convert, node.args)
      return global_scope[node.func.id](*args)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
      return vars_convert(node.left) + vars_convert(node.right)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
      return vars_convert(node.left) % vars_convert(node.right)
    elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
      if len(node.values) != 2:
        raise ValueError(
            'invalid "or": exactly 2 operands required (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      return vars_convert(node.values[0]) or vars_convert(node.values[1])
    elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
      if len(node.values) != 2:
        raise ValueError(
            'invalid "and": exactly 2 operands required (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      return vars_convert(node.values[0]) and vars_convert(node.values[1])
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
      return not vars_convert(node.operand)
    else:
      raise ValueError(
          'unexpected AST node: %s %s (file %r, line %s)' % (
              node, ast.dump(node), filename,
              getattr(node, 'lineno', '<unknown>')))
  return _convert(node_or_string)


def _gclient_exec(node_or_string, global_scope, filename='<unknown>'):
  """Safely execs a set of assignments. Returns resulting scope."""
  result_scope = {}

  if isinstance(node_or_string, basestring):
    node_or_string = ast.parse(node_or_string, filename=filename, mode='exec')
  if isinstance(node_or_string, ast.Expression):
    node_or_string = node_or_string.body

  def _visit_in_module(node):
    if isinstance(node, ast.Assign):
      if len(node.targets) != 1:
        raise ValueError(
            'invalid assignment: use exactly one target (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      target = node.targets[0]
      if not isinstance(target, ast.Name):
        raise ValueError(
            'invalid assignment: target should be a name (file %r, line %s)' % (
                filename, getattr(node, 'lineno', '<unknown>')))
      value = _gclient_eval(
          node.value, global_scope, filename=filename,
          expose_vars=(target.id == 'vars'))

      if target.id in result_scope:
        raise ValueError(
            'invalid assignment: overrides var %r (file %r, line %s)' % (
                target.id, filename, getattr(node, 'lineno', '<unknown>')))

      result_scope[target.id] = value
    else:
      raise ValueError(
          'unexpected AST node: %s %s (file %r, line %s)' % (
              node, ast.dump(node), filename,
              getattr(node, 'lineno', '<unknown>')))

  if isinstance(node_or_string, ast.Module):
    for stmt in node_or_string.body:
      _visit_in_module(stmt)
  else:
    raise ValueError(
        'unexpected AST node: %s %s (file %r, line %s)' % (
            node_or_string,
            ast.dump(node_or_string),
            filename,
            getattr(node_or_string, 'lineno', '<unknown>')))

  return result_scope


class CheckFailure(Exception):
  """Contains details of a check failure."""
  def __init__(self, msg, path, exp, act):
    super(CheckFailure, self).__init__(msg)
    self.path = path
    self.exp = exp
    self.act = act


def Check(content, path, global_scope, expected_scope):
  """Cross-checks the old and new gclient eval logic.

  Safely execs |content| (backed by file |path|) using |global_scope|,
  and compares with |expected_scope|.

  Throws CheckFailure if any difference between |expected_scope| and scope
  returned by new gclient eval code is detected.
  """
  def fail(prefix, exp, act):
    raise CheckFailure(
        'gclient check for %s:  %s exp %s, got %s' % (
            path, prefix, repr(exp), repr(act)), prefix, exp, act)

  def compare(expected, actual, var_path, actual_scope):
    if isinstance(expected, dict):
      exp = set(expected.keys())
      act = set(actual.keys())
      if exp != act:
        fail(var_path, exp, act)
      for k in expected:
        compare(expected[k], actual[k], var_path + '["%s"]' % k, actual_scope)
      return
    elif isinstance(expected, list):
      exp = len(expected)
      act = len(actual)
      if exp != act:
        fail('len(%s)' % var_path, expected_scope, actual_scope)
      for i in range(exp):
        compare(expected[i], actual[i], var_path + '[%d]' % i, actual_scope)
    else:
      if expected != actual:
        fail(var_path, expected_scope, actual_scope)

  result_scope = _gclient_exec(content, global_scope, filename=path)

  compare(expected_scope, result_scope, '', result_scope)

  _GCLIENT_SCHEMA.validate(result_scope)

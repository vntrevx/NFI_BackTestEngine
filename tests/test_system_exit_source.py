from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.full_vector_runtime import _retained_trade_features
from nfi_backtest_engine.x7.custom_exit_prefix import compile_custom_exit_prefix
from nfi_backtest_engine.x7.system_exit_ir import compile_system_exit_programs

_ROOT = Path('benchmarks/fixtures/source-contracts/x8-system-v4')


def _inputs() -> tuple[dict[str, ast.FunctionDef], dict]:
    methods, constants = {}, {}
    for name in ('dispatch.source', 'exits.source'):
        strategy = next(node for node in ast.parse((_ROOT / name).read_text()).body
                        if isinstance(node, ast.ClassDef))
        methods.update({node.name: node for node in strategy.body
                        if isinstance(node, ast.FunctionDef)})
        constants.update({node.targets[0].id: ast.literal_eval(node.value)
                          for node in strategy.body if isinstance(node, ast.Assign)
                          and isinstance(node.targets[0], ast.Name)})
    return methods, constants


def test_exit_programs_bind_the_unchanged_donor_methods() -> None:
    methods, constants = _inputs()
    assert compile_system_exit_programs(methods, constants) == json.loads(
        (_ROOT / 'exit-programs.json').read_text())
    assert compile_custom_exit_prefix(methods, constants) == json.loads(
        (_ROOT / 'prefix-program.json').read_text())
    source = _ROOT / 'exits.source'
    provenance = json.loads((_ROOT / 'exits-provenance.json').read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance['contract_sha256']
    lines = source.read_text().splitlines(keepends=True)
    for name, expected in provenance['methods'].items():
        method = methods[name]
        segment = ''.join(lines[method.lineno - 1:method.end_lineno])
        assert hashlib.sha256(segment.encode()).hexdigest() == expected
    geometry = methods['_bad_trade_controller_ema_gap_and_adverse_move']
    assert [ast.unparse(node) for node in geometry.decorator_list] == ['staticmethod']


@pytest.mark.parametrize('mutation', ['cost', 'system', 'derisk-scan', 'clock', 'early-return'])
def test_exit_assumption_changes_fail_closed(mutation: str) -> None:
    methods, constants = _inputs()
    if mutation == 'cost':
        method = methods['long_exit_stoploss']
        assignment = next(node for node in method.body if isinstance(node, ast.Assign)
                          and isinstance(node.targets[0], ast.Name)
                          and node.targets[0].id == 'entry_cost')
        assignment.value = ast.Constant(value=1.0)
    elif mutation == 'system':
        constants['system_name_use'] = 'undeclared-system'
    elif mutation == 'derisk-scan':
        method = methods['exit_profit_target']
        call = next(node for node in ast.walk(method) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'partition')
        call.func.attr = 'split'
    elif mutation == 'clock':
        method = methods['_bad_trade_controller_exit_reason']
        clock = next(node for node in ast.walk(method) if isinstance(node, ast.Attribute)
                     and node.attr == 'open_date_utc')
        clock.attr = 'close_date_utc'
    else:
        methods['custom_exit'].body.insert(0, ast.Return(value=ast.Constant('extra_exit')))
    with pytest.raises(StrategyAnalysisError):
        compile_system_exit_programs(methods, constants)
        compile_custom_exit_prefix(methods, constants)


def test_source_threshold_changes_change_program_and_optional_reads_stay_optional() -> None:
    methods, constants = _inputs()
    constants['system_v4_stops_enable'] = True
    original = compile_system_exit_programs(methods, constants)
    constants['system_v4_stop_threshold_doom_futures'] = 0.41
    assert compile_system_exit_programs(methods, constants) != original
    prefix = compile_custom_exit_prefix(methods, constants)
    hot_ir = {'nfi_trade_manager': {'operation': {'custom_exit_prefix': prefix}}}
    assert _retained_trade_features(hot_ir) == []
    assert _retained_trade_features(hot_ir, available_columns={'EMA_50_1d', 'unused'}) == [
        'EMA_50_1d']


def test_official_callback_vectors_are_bound_to_source_and_configuration() -> None:
    evidence = json.loads(Path(
        'benchmarks/evidence/x8-system-v4-2026-09-07/official-exit-cases-v2.json'
    ).read_text())
    methods, constants = _inputs()
    for name, sha in evidence['sources'].items():
        assert hashlib.sha256((_ROOT / name).read_bytes()).hexdigest() == sha
    variants = json.loads((_ROOT / 'exit-variants.json').read_text())
    for name, overrides in evidence['variants'].items():
        selected = constants | overrides
        assert variants[name] == {
            'exits': compile_system_exit_programs(methods, selected),
            'prefix': compile_custom_exit_prefix(methods, selected),
        }
    assert len(evidence['cases']) == 337

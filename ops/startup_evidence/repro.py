import ast
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


BASE = Path(__file__).resolve().parent
VERIFIER = BASE / 'fixtures/original_verifier.txt'


def load_timeline(path):
    source = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    names = {'finite', 'StartupPhase', 'Startup', 'ColdProgress', 'Timeline'}
    definitions = [node for node in source.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    namespace = dict(math=math, re=re, dataclass=dataclass, replace=replace, Enum=Enum)
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['Timeline'], source


def row_at(offset, requested=16, complete=True):
    now = 100.0 + offset
    pending = requested != 16
    return dict(ts=1000.0 + offset, snapshot_at=1000.0 + offset, mono=now,
                readback_mono=now - .5, execution_deadline=now + 60 if pending else None,
                destination_requested_at=now if pending else None,
                requested_at=now if pending else None, gear=16, requested=requested,
                rpm=2095, execution='pending' if pending else 'matched', confirmed=not pending,
                nodes=[dict(host=host, source_ts=1000.0 + offset)
                       for host in ('192.168.3.180', '192.168.3.181',
                                    '192.168.3.183', '192.168.3.184')],
                telemetry_complete=True, thermal_complete=complete, service_handoff=False,
                scenario='trial_pending' if pending else 'protect',
                scenario_detail=('启动证据30s完成，相邻请求15' if pending else
                                 '启动新基线 earned=0/30s，完整匹配确认后才允许下降'))


def observation_loop(source):
    for node in source.body:
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and node.target.id == 'index'):
            return compile(ast.Module(body=[node], type_ignores=[]), str(VERIFIER), 'exec')
    raise AssertionError('Original live observation loop not found')


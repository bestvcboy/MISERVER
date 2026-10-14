import ast
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


BASE = Path(__file__).resolve().parent
FROZEN_HASH = '81c1e0d356d598a0c7ed717e4f9bea94c99267a81a7eca7f66b707f213e22df4'
TRACE_HASH = '9cced96b8f11ad4e44f1d4fe521753e57fdc946169b65a7622fd2c70213540df'
REJECTION = 'Startup descent before30 seconds of complete matched16 baseline'


@dataclass(frozen=True)
class Poll:
    at: float
    index: int


def load():
    raw = (BASE / 'fixtures/original_verifier.txt').read_bytes()
    trace = (BASE / 'fixtures/controller_trace.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_HASH
    assert hashlib.sha256(trace).hexdigest() == TRACE_HASH
    names = {'finite', 'StartupPhase', 'Startup', 'ColdProgress', 'Timeline'}
    nodes = [node for node in ast.parse(raw).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    scope = dict(math=math, re=re, dataclass=dataclass, replace=replace, Enum=Enum)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<frozen-timeline>', 'exec'), scope)
    payload = json.loads(trace)
    assert payload['count'] == 22 and payload['samples_truncated'] is False
    rows = payload['trace']
    assert len(rows) == 22
    assert all(a['mono'] < b['mono'] for a, b in zip(rows, rows[1:]))
    return scope['Timeline'], rows


def projection(sample, poll, end):
    pending = sample['execution'] == 'pending'
    return dict(ts=poll.at, snapshot_at=poll.at, mono=poll.at,
                readback_mono=sample['readback'], gear=sample['actual'],
                requested=sample['requested'], execution=sample['execution'],
                confirmed=sample['confirmed'],
                telemetry_complete=True, thermal_complete=sample['thermal_complete'],
                service_handoff=False, scenario='trial_pending' if pending else 'protect',
                scenario_detail='启动新基线 earned=0/30s，完整匹配确认后才允许下降',
                requested_at=end if pending else None,
                destination_requested_at=end if pending else None,
                execution_deadline=end + 60 if pending else None,
                nodes=[dict(host='slot' + str(index), source_ts=stamp)
                       for index, stamp in enumerate(sample['sources_advance'])])


def evaluate(timeline_type, samples, polls):
    timeline = timeline_type()
    indexes = []
    first_complete = None
    for poll in polls:
        sample = samples[poll.index]
        indexes.append(poll.index)
        if sample['thermal_complete'] and first_complete is None:
            first_complete = poll.index
        row = projection(sample, poll, samples[-1]['mono'])
        try:
            timeline.add(row)
        except AssertionError as error:
            if str(error) != REJECTION:
                raise
            attempted = None
            traceback = error.__traceback__
            while traceback is not None:
                if traceback.tb_frame.f_code is timeline_type.add.__code__:
                    attempted = traceback.tb_frame.f_locals['startup'].earned
                traceback = traceback.tb_next
            assert attempted is not None
            return dict(verdict='startup_rejected', earned=attempted,
                        first_complete_index=first_complete, source_indexes=indexes)
    assert timeline.startup.phase.value == 'closed'
    return dict(verdict='accepted_counter_only', earned=timeline.startup.earned,
                first_complete_index=first_complete, source_indexes=indexes)


def schedule(samples, phase):
    polls = []
    index = 0
    first, end = samples[0]['mono'], samples[-1]['mono']
    for count in range(40):
        at = first + phase + 3 * count
        while index + 1 < len(samples) and samples[index + 1]['mono'] <= at:
            index += 1
        polls.append(Poll(at, index))
        if at >= end:
            return polls
    raise AssertionError('bounded schedule did not cover final request')


def main():
    timeline_type, samples = load()
    direct = evaluate(timeline_type, samples,
                      [Poll(sample['mono'], index) for index, sample in enumerate(samples)])
    assert direct['verdict'] == 'accepted_counter_only'
    assert abs(direct['earned'] - 30.723721185000613) < 1e-9
    cases = []
    for tenth in range(30):
        phase = tenth / 10
        result = evaluate(timeline_type, samples, schedule(samples, phase))
        cases.append(dict(phase_seconds=phase, **result))
    rejected = sum(case['verdict'] == 'startup_rejected' for case in cases)
    print(json.dumps(dict(scope='constructed counter-only observer schedules, not original live proof',
                          fixed_source_hash=FROZEN_HASH, recorded_trace_hash=TRACE_HASH,
                          assumptions=['publication assumed at stored controller mono',
                                       'telemetry_complete true for fixture',
                                       'snapshot wall and mono represented by synthetic poll time',
                                       'trial stamps/deadline and scenario strings are fixture metadata',
                                       'source slots preserve array position, not verified host identities',
                                       'snapshot guards/readiness/physical execution are not run'],
                          direct=direct, rejected=rejected, accepted=len(cases)-rejected,
                          cases=cases), sort_keys=True))


if __name__ == '__main__':
    main()

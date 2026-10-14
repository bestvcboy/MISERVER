import ast
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import startup_evidence as evidence


BASE = Path(__file__).resolve().parent
FROZEN = BASE / 'fixtures/original_verifier.txt'
CANDIDATE = BASE / 'verify-graded-cooling-live.py'
FROZEN_SHA256 = '81c1e0d356d598a0c7ed717e4f9bea94c99267a81a7eca7f66b707f213e22df4'
spec = importlib.util.spec_from_file_location('r6_original_repro', BASE / 'repro.py')
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)
Timeline, original_ast = repro.load_timeline(FROZEN)
candidate_timeline, candidate_ast = repro.load_timeline(CANDIDATE)


def snapshot_namespace():
    names = {'age', 'snapshot'}
    definitions = [node for node in original_ast.body if isinstance(node, ast.FunctionDef) and node.name in names]
    clock = SimpleNamespace(time=lambda: 1030.0, monotonic=lambda: 130.0,
                            monotonic_ns=lambda: 130_000_000_001)
    namespace = dict(check_deadline=lambda: None, finite=Timeline.add.__globals__['finite'],
                     time=clock, _window=SimpleNamespace(start_ns=100_000_000_000, end_ns=130_000_000_000))
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(FROZEN), 'exec'), namespace)
    return namespace


SNAPSHOT = snapshot_namespace()


def status_at(offset=30, initial=False):
    row = repro.row_at(offset)
    wall, mono = row['ts'], row['mono']
    nodes = [dict(host=host, source_ts=wall, sampled_at=wall, requested_mono=mono,
                  online=True, cpu_temp=40, gpu_temp=40, board_temp=40,
                  power_w=30, power_source='fixture', cpu_load=0, gpu_load=0)
             for host in evidence.HOSTS]
    fan = dict(configured=True, fault=False, power='on', mode='favorite', level=16,
               rpm=2095, verified_at=wall, observed_mono=mono - .5, execution='matched',
               confirmed=True, ambient_c=20, ambient_source='purifier:70632677:temp_dec', ambient_verified_at=wall)
    decision = dict(level=16, retry_blocked=False, scenario='protect',
                    scenario_detail='启动新基线 earned=0/30s，完整匹配确认后才允许下降', reasons=[],
                    trend_complete={host: True for host in evidence.HOSTS})
    if initial:
        fan.update(configured=False, confirmed=False, level=None, rpm=None, error=None)
        decision = dict(level=16, reasons=['启动全风保护'])
    return dict(server_time=wall, controller_age_seconds=0, storage_error=None, settings_error=None,
                learning=dict(error=None), settings=dict(retention_days=7, max_log_mb=4096, manual_level=None),
                dropped_records=0, requested_level=16, nodes=nodes, fan=fan, decision=decision)


def decoded(path):
    source = io.BytesIO(path.read_bytes())
    events = []
    while True:
        header = source.read(evidence.FRAME_BYTES)
        if len(header) != evidence.FRAME_BYTES:
            return events
        length = struct.unpack('>I', header[:4])[0]
        if length > evidence.MAX_EVENT_BYTES - evidence.FRAME_BYTES:
            return events
        payload = source.read(length)
        if len(payload) != length or hashlib.sha256(payload).digest() != header[4:]:
            return events
        events.append(json.loads(payload))


def earned(startup):
    return startup[2][3][1]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.temporary.cleanup)
        self.counter = 0

    def run_owner(self):
        self.counter += 1
        path = Path(self.temporary.name) / ('journal' + str(self.counter))
        evidence._pin(SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND | getattr(os, 'O_BINARY', 0), 0o600)
        run = evidence.EvidenceRun(descriptor, path, evidence.time.time() + 60,
                                   SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)
        self.addCleanup(lambda: run.finish() if not run._closed else None)
        return run

    def seeded(self):
        timeline = Timeline()
        for offset in range(0, 30, 3):
            timeline.add(repro.row_at(offset, complete=offset >= 3))
        self.assertEqual(timeline.startup.earned, 24)
        return timeline

    def test_failed_candidate_live_loop_private_retention_original_list_still_zero(self):
        run = self.run_owner()
        timeline = self.seeded()
        records = []
        namespace = dict(duration=SimpleNamespace(status_samples=1), check_deadline=lambda: None,
                         request=lambda route: '{}', snapshot=lambda status, hosts: repro.row_at(30, requested=15),
                         hosts=None, timeline=timeline, records=records, json=json, evidence=run, Stage=evidence.Stage)
        calls = []
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code is Timeline.add.__code__:
                calls.append(True)
        previous_profile = sys.getprofile()
        try:
            sys.setprofile(profile)
            with self.assertRaisesRegex(AssertionError, 'Startup descent before30 seconds'):
                exec(repro.observation_loop(candidate_ast), namespace)
        finally:
            sys.setprofile(previous_profile)
        self.assertEqual(calls, [True])
        self.assertEqual(records, [])
        events = decoded(run.path)
        before = [event[2] for event in events if event[0] == evidence.Event.BEFORE]
        after = [event[2] for event in events if event[0] == evidence.Event.AFTER]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0][3], evidence.Verdict.REJECTED)
        self.assertEqual((earned(before[0][3][0]), earned(after[0][6][1]), earned(after[0][7][0])), (24, 27, 24))
        self.assertEqual(after[0][5][1][evidence.ROW_NUMBERS.index('mono')], [2, 130.0])
        self.assertEqual(timeline.previous['mono'], 127)
        self.assertEqual(timeline.pending_deadline, 190)
        self.assertEqual(timeline.startup.deadline, 190)
        with self.assertRaises(evidence.EvidenceFailure):
            run.accept()

    def test_exact_original_timeline_rejected_evidence_for_all_three_stages(self):
        for stage in evidence.Stage:
            with self.subTest(stage=stage):
                run = self.run_owner()
                timeline = self.seeded()
                with self.assertRaisesRegex(AssertionError, 'Startup descent before30 seconds'):
                    with run.observe(stage, {}, timeline) as observation:
                        row = repro.row_at(30, requested=15)
                        observation.derived(row)
                        timeline.add(row)
                before, after = decoded(run.path)[1:]
                self.assertEqual(before[2][0], stage)
                self.assertEqual(after[2][0], stage)
                self.assertEqual((earned(before[2][3][0]), earned(after[2][6][1]), earned(after[2][7][0])), (24, 27, 24))

    def test_success_retains_committed_state_and_does_not_accept_run(self):
        run = self.run_owner()
        timeline = Timeline()
        row = repro.row_at(0)
        with run.observe(evidence.Stage.READINESS, {}, timeline) as observation:
            observation.derived(row)
            timeline.add(row)
        after = decoded(run.path)[-1][2]
        self.assertEqual(after[3], evidence.Verdict.ACCEPTED)
        self.assertEqual(after[6][1], after[7][0])
        self.assertEqual(after[7][0][1], [2, 1])
        run.finish()
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.INCOMPLETE)

    def test_readiness_initial_none_actual_original_snapshot(self):
        run = self.run_owner()
        with run.observe(evidence.Stage.READINESS, status_at(initial=True), Timeline()) as observation:
            row = SNAPSHOT['snapshot'](status_at(initial=True), set(evidence.HOSTS), incomplete=True, initializing=True)
            self.assertIsNone(row)
            observation.derived(row)
        after = decoded(run.path)[-1][2]
        self.assertEqual(after[2], evidence.Evaluation.INITIALIZING)
        self.assertEqual(after[6], [evidence.Attempt.NOT_REACHED])
        self.assertEqual(after[7][0], [evidence.Fact.MISSING])

    def test_exact_readiness_loop_keeps_initial_none_continue_and_15_attempts(self):
        run = self.run_owner()
        loop = next(node for node in candidate_ast.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == 'attempt')
        requests = []
        def request(route):
            requests.append(route)
            return json.dumps(status_at(initial=True))
        namespace = dict(check_deadline=lambda: None, request=request, json=json, hosts=None,
                         snapshot=SNAPSHOT['snapshot'], initial_motor_seen=False, initializing_seen=False,
                         readiness_started=130.0, time=SimpleNamespace(monotonic=lambda: 130.0, sleep=lambda delay: None),
                         timeout=lambda limit: limit, timeline=Timeline(), evidence=run, Stage=evidence.Stage)
        with self.assertRaisesRegex(AssertionError, 'bounded startup window'):
            with run:
                exec(compile(ast.Module(body=[loop], type_ignores=[]), '<candidate-readiness>', 'exec'), namespace)
        self.assertEqual(requests, ['/api/status'] * 15)
        after = [event[2] for event in decoded(run.path) if event[0] == evidence.Event.AFTER]
        self.assertEqual(len(after), 15)
        self.assertTrue(all(facts[2] == evidence.Evaluation.INITIALIZING and facts[6] == [evidence.Attempt.NOT_REACHED] for facts in after))
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)
        self.assertEqual(decoded(run.path)[-1][2][-1], [15, 0, 0])

    def test_exact_history_loop_future_guard_retained_before_snapshot(self):
        run = self.run_owner()
        loop = next(node for node in candidate_ast.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == 'stored')
        namespace = dict(rows=[dict(snapshot_at=2000.0)], time=SimpleNamespace(time=lambda: 1000.0),
                         history_timeline=Timeline(), history_records=[], hosts=set(evidence.HOSTS),
                         snapshot=lambda *args, **kwargs: self.fail('future guard must precede snapshot'),
                         evidence=run, Stage=evidence.Stage)
        with self.assertRaisesRegex(AssertionError, 'Future historical snapshot'):
            exec(compile(ast.Module(body=[loop], type_ignores=[]), '<candidate-history>', 'exec'), namespace)
        before, after = decoded(run.path)[1:]
        self.assertEqual(before[2][2][1][evidence.INPUT_NUMBERS.index('snapshot_at')], [2, 2000.0])
        self.assertEqual(after[2][2], evidence.Evaluation.NOT_REACHED)
        self.assertEqual(namespace['history_records'], [])

    def test_snapshot_refusal_captures_exact_pinned_clocks_and_safe_facts(self):
        run = self.run_owner()
        status = status_at()
        status['fan']['rpm'] = 0
        with self.assertRaisesRegex(AssertionError, 'Motor is not turning'):
            with run.observe(evidence.Stage.LIVE, status, Timeline()) as observation:
                observation.derived(SNAPSHOT['snapshot'](status, set(evidence.HOSTS)))
        before, after = decoded(run.path)[1:]
        self.assertEqual(before[2][2][3][1][1], [2, 0])
        facts = after[2]
        self.assertEqual(facts[2], evidence.Evaluation.SNAPSHOT_REFUSED)
        self.assertIsNone(facts[5])
        self.assertEqual(facts[6], [evidence.Attempt.NOT_REACHED])
        self.assertEqual(facts[8][:4], [[2, 1030.0], [2, 130.0], [2, 130_000_000_001], [2, 1]])

    def test_historical_snapshot_refusal_preserves_exact_fraction_clock(self):
        run = self.run_owner()
        mono = 259949.65127965
        status = status_at()
        status.update(snapshot_at=1030.0, ts=1030.0, control=dict(manual_level=None))
        status['learning'] = dict(mono=mono, healthy=True, dropped_records=0)
        for node in status['nodes']:
            node['requested_mono'] = mono
        status['fan'].update(observed_mono=mono - .5, rpm=0)
        with patch.object(SNAPSHOT['time'], 'monotonic', return_value=mono):
            with self.assertRaisesRegex(AssertionError, 'Motor is not turning'):
                with run.observe(evidence.Stage.HISTORY, status, Timeline()) as observation:
                    observation.derived(SNAPSHOT['snapshot'](status, set(evidence.HOSTS), historical=True))
        numerator, denominator = mono.as_integer_ratio()
        clocks = decoded(run.path)[-1][2][8]
        self.assertEqual(clocks[1:4], [[2, mono], [2, numerator * 1_000_000_000], [2, denominator]])
        self.assertEqual(clocks[4:6], [[2, 1030.0], [2, mono]])

    def test_malformed_input_not_reached_no_fabricated_row(self):
        run = self.run_owner()
        with self.assertRaises(TypeError):
            with run.observe(evidence.Stage.READINESS, None, Timeline()) as observation:
                observation.derived(SNAPSHOT['snapshot'](None))
        after = decoded(run.path)[-1][2]
        self.assertEqual(after[2], evidence.Evaluation.SNAPSHOT_REFUSED)
        self.assertIsNone(after[5])
        self.assertEqual(after[8], [[0]] * len(evidence.CLOCK_NUMBERS))

    def test_unknown_arbitrary_frame_locals_exception_text_never_saved(self):
        run = self.run_owner()
        secret = 'CREDENTIAL-MUST-NOT-SURVIVE'
        status = status_at()
        status.update(token=secret, error=secret, body=secret * 100000)
        status['fan']['error'] = secret
        status['decision'].update(reasons=[secret], scenario_detail=secret, scenario=secret)
        status['nodes'][0]['power_source'] = secret
        status['nodes'][0]['host'] = secret
        def arbitrary():
            startup = secret
            token = secret
            raise RuntimeError(secret + startup + token)
        with self.assertRaises(RuntimeError):
            with run.observe(evidence.Stage.LIVE, status, Timeline()):
                arbitrary()
        self.assertNotIn(secret.encode(), run.path.read_bytes())
        after = decoded(run.path)[-1][2]
        self.assertEqual(after[2], evidence.Evaluation.NOT_REACHED)
        self.assertEqual(after[6], [evidence.Attempt.NOT_REACHED])

    def test_fact_states_strict_numbers_precision_and_traversal(self):
        self.assertEqual(evidence._number(evidence._MISSING), [0])
        for value in (None, True, float('nan'), float('inf'), 10 ** 100000, 1 << 127, '123'):
            self.assertEqual(evidence._number(value), [1])
        for value in (0, -(1 << 63), (1 << 63) - 1, 30.723721185000613):
            self.assertEqual(evidence._number(value), [2, value])
        self.assertEqual(evidence._flag(1), [1])
        class Hostile(dict):
            def get(self, key, default=None):
                raise AssertionError('arbitrary traversal')
        class Huge(list):
            def __iter__(self):
                raise AssertionError('arbitrary traversal')
        self.assertEqual(evidence._input(Hostile())[0], evidence.Fact.INVALID)
        self.assertTrue(evidence._hosts(Huge())[0])
        self.assertTrue(evidence._hosts([{}] * 100000)[0])
        duplicate = evidence._hosts([{'host': evidence.HOSTS[0]}, {'host': evidence.HOSTS[0]}])
        self.assertEqual(duplicate[1][0][0], evidence.Fact.INVALID)
        trend = {host: True for host in evidence.HOSTS}
        projected = evidence._hosts(status_at()['nodes'], trend)
        self.assertEqual([node[3] for node in projected[1]], [[2, True]] * 4)
        trend[evidence.HOSTS[1]] = None
        del trend[evidence.HOSTS[2]]
        projected = evidence._hosts([], trend)
        self.assertEqual([node[3] for node in projected[1]][1:3], [[1], [0]])

    def test_before_fsync_precedes_evaluation_invoked_once(self):
        run = self.run_owner()
        synchronized = []
        original_fsync = os.fsync
        def sync(fd):
            original_fsync(fd)
            synchronized.append(True)
        count = 0
        with patch.object(evidence.os, 'fsync', side_effect=sync):
            with run.observe(evidence.Stage.LIVE, {}, Timeline()) as observation:
                self.assertEqual(len(synchronized), 1)
                count += 1
                observation.derived(None)
        self.assertEqual(count, 1)
        self.assertEqual(len(synchronized), 2)

    def test_failed_before_does_not_invoke_evaluator_and_bypasses_oserror(self):
        run = self.run_owner()
        invoked = []
        with patch.object(evidence.os, 'fsync', side_effect=OSError('secret disk')):
            with self.assertRaises(evidence.EvidenceFailure) as caught:
                try:
                    with run.observe(evidence.Stage.READINESS, {}, Timeline()):
                        invoked.append(True)
                except OSError:
                    self.fail('readiness swallowed evidence failure')
        self.assertFalse(invoked)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        with self.assertRaises(evidence.EvidenceFailure):
            run.accept()

    def test_after_io_failure_chains_original_rejection(self):
        run = self.run_owner()
        timeline = self.seeded()
        original = os.fsync
        calls = []
        def sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('secret disk')
            original(fd)
        with patch.object(evidence.os, 'fsync', side_effect=sync):
            with self.assertRaises(evidence.EvidenceFailure) as caught:
                with run.observe(evidence.Stage.LIVE, {}, timeline) as observation:
                    row = repro.row_at(30, requested=15)
                    observation.derived(row)
                    timeline.add(row)
        self.assertIsInstance(caught.exception.__cause__, AssertionError)
        self.assertIn('Startup descent before30', str(caught.exception.__cause__))
        self.assertNotIn(b'secret disk', run.path.read_bytes())

    def test_after_io_failure_on_normal_return_refuses_acceptance(self):
        run = self.run_owner()
        calls = 0
        original = os.fsync
        def sync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError()
            original(fd)
        with patch.object(evidence.os, 'fsync', side_effect=sync):
            with self.assertRaises(evidence.EvidenceFailure):
                with run.observe(evidence.Stage.LIVE, {}, Timeline()) as observation:
                    observation.derived(None)
        with self.assertRaises(evidence.EvidenceFailure):
            run.accept()

    def test_retryable_readiness_oserror_retained_and_retry_allowed(self):
        run = self.run_owner()
        with self.assertRaises(OSError):
            with run.observe(evidence.Stage.READINESS, {}, Timeline()):
                raise OSError('not saved')
        with run.observe(evidence.Stage.READINESS, {}, Timeline()) as observation:
            observation.derived(None)
        run.accept()
        run.finish()
        events = decoded(run.path)
        self.assertEqual(events[2][2][3:5], [evidence.Verdict.REJECTED, evidence.Category.IO])
        self.assertEqual(events[-1][2][0], evidence.Verdict.ACCEPTED)

    def test_count_and_total_size_reserve_terminal_and_stop_before_evaluation(self):
        for field, value in (('_count', evidence.MAX_EVENTS - 3),
                             ('_bytes', evidence.MAX_BYTES - evidence.TERMINAL_RESERVE - 2 * evidence.MAX_EVENT_BYTES + 1)):
            run = self.run_owner()
            setattr(run, field, value)
            with self.assertRaises(evidence.EvidenceFailure):
                run.observe(evidence.Stage.LIVE, {}, Timeline())
            run.finish()
            self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)

    def test_oversize_single_event_refuses_before_eval(self):
        run = self.run_owner()
        count = run._count
        with self.assertRaises(evidence.EvidenceFailure):
            run._emit(evidence.Event.BEFORE, [0] * evidence.MAX_EVENT_BYTES)
        self.assertEqual(run._count, count)
        self.assertLessEqual(run.path.stat().st_size, evidence.MAX_EVENT_BYTES)

    def test_partial_write_and_zero_write(self):
        run = self.run_owner()
        original = os.write
        with patch.object(evidence.os, 'write', side_effect=lambda fd, data: original(fd, data[:7])):
            with run.observe(evidence.Stage.READINESS, {}, Timeline()) as observation:
                observation.derived(None)
        self.assertEqual(len(decoded(run.path)), 3)
        with patch.object(evidence.os, 'write', return_value=0):
            with self.assertRaises(evidence.EvidenceFailure):
                run.observe(evidence.Stage.LIVE, {}, Timeline()).__enter__()

    def test_early_add_assertion_attempt_not_reached(self):
        run = self.run_owner()
        timeline = self.seeded()
        row = repro.row_at(0)
        with self.assertRaisesRegex(AssertionError, 'clock went backwards'):
            with run.observe(evidence.Stage.HISTORY, {}, timeline) as observation:
                observation.derived(row)
                timeline.add(row)
        self.assertEqual(decoded(run.path)[-1][2][6], [evidence.Attempt.NOT_REACHED])

    def test_actual_later_snapshot_clock_precision_preserved(self):
        run = self.run_owner()
        clocks = SNAPSHOT['time']
        precise = 130.12345678901234
        with patch.object(clocks, 'monotonic', return_value=precise):
            with run.observe(evidence.Stage.LIVE, status_at(), Timeline()) as observation:
                row = SNAPSHOT['snapshot'](status_at(), set(evidence.HOSTS))
                observation.derived(row)
        after = decoded(run.path)[-1][2]
        index = evidence.ROW_NUMBERS.index('mono')
        self.assertEqual(after[5][1][index], [2, precise])
        self.assertNotEqual(after[5][1][index][1], 130.0)

    def test_cleanup_rejects_uncaught_failure_and_preserves_original_if_disk_fails(self):
        run = self.run_owner()
        original = KeyboardInterrupt('secret')
        run.finish(original)
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)
        run = self.run_owner()
        with patch.object(evidence.os, 'fsync', side_effect=OSError()):
            with self.assertRaises(evidence.EvidenceFailure) as caught:
                run.finish(original)
        self.assertIs(caught.exception.__cause__, original)
        self.assertTrue(run._closed)

    def test_uncaught_hook_and_atexit_use_same_owner(self):
        run = self.run_owner()
        emitted = []
        original = sys.excepthook
        with patch.object(sys, 'excepthook', side_effect=lambda *args: emitted.append(args)), patch.object(evidence.atexit, 'register') as register:
            run.install_cleanup()
            register.assert_called_once_with(run.finish)
            sys.excepthook(ValueError, ValueError('secret'), None)
            self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)
            self.assertEqual(len(emitted), 1)
        self.assertIs(sys.excepthook, original)

    def test_explicit_accept_only_then_rejects_failure_after_accept(self):
        run = self.run_owner()
        run.accept()
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.ACCEPTED)
        run.finish(AssertionError('completion'))
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)

    def test_candidate_tail_seal_and_close_failure_nonzero_no_verified_output(self):
        script = '''
import ast, sys
from unittest.mock import patch
import test_startup_evidence as t
case = t.EvidenceTests()
case.setUp()
run = case.run_owner()
tail = compile(ast.Module(body=t.candidate_ast.body[-5:], type_ignores=[]), '<candidate-tail>', 'exec')
original_close = t.evidence.os.close
def close(fd):
    original_close(fd)
    raise OSError('lifecycle close failure')
try:
    with patch.object(t.evidence.os, sys.argv[1], side_effect=close if sys.argv[1] == 'close' else OSError('lifecycle seal failure')):
        try:
            exec(tail, dict(evidence=run, check_deadline=lambda: None, summary='{"verified": true}'))
        except t.evidence.EvidenceFailure as failure:
            assert isinstance(failure.__cause__, OSError)
            raise
finally:
    case.doCleanups()
'''
        for operation in ('fsync', 'close'):
            with self.subTest(operation=operation):
                result = subprocess.run([sys.executable, '-B', '-c', script, operation], cwd=BASE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'EvidenceFailure', result.stderr)
                self.assertIn(b'OSError: lifecycle', result.stderr)
                self.assertNotIn(b'AssertionError', result.stderr)

    def test_close_failure_preserves_primary_error_and_suppresses_success(self):
        run = self.run_owner()
        primary = AssertionError('original check')
        original_close = os.close
        def close(fd):
            original_close(fd)
            raise OSError('close')
        with patch.object(evidence.os, 'close', side_effect=close):
            with self.assertRaises(evidence.EvidenceFailure) as caught:
                run.finish(primary)
        self.assertIs(caught.exception.__cause__, primary)

    def test_prefix_decoder_torn_corrupt_count_size_traversal_no_repair(self):
        run = self.run_owner()
        with run.observe(evidence.Stage.LIVE, {}, Timeline()) as observation:
            observation.derived(None)
        run.finish()
        source = run.path.read_bytes()
        for cut in (1, 10, 36, 50):
            self.assertLess(len(list(evidence._read_frames(io.BytesIO(source[:-cut])))), 4)
        broken = bytearray(source)
        broken[-1] ^= 1
        self.assertLess(len(list(evidence._read_frames(io.BytesIO(broken)))), 4)
        self.assertEqual(run.path.read_bytes(), source)
        self.assertEqual(list(evidence._read_frames(io.BytesIO(struct.pack('>I', 4097) + bytes(32)))), [])
        def frame(value):
            payload = json.dumps(value, separators=(',', ':')).encode()
            return struct.pack('>I', len(payload)) + hashlib.sha256(payload).digest() + payload
        for facts in ('secret', [0] * 33, [[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]], float('nan'), {'token': 'secret'}):
            self.assertEqual(list(evidence._read_frames(io.BytesIO(frame([0, 0, facts])))), [])
        many = b''.join(frame([0, sequence, []]) for sequence in range(evidence.MAX_EVENTS + 1))
        stream = io.BytesIO(many)
        self.assertEqual(len(list(evidence._read_frames(stream))), evidence.MAX_EVENTS)
        self.assertLess(stream.tell(), len(many))
        self.assertEqual(list(evidence._read_frames(io.BytesIO(frame([0, 1, []])))), [])
        full = io.BytesIO()
        for sequence in range(evidence.MAX_BYTES // evidence.MAX_EVENT_BYTES + 1):
            payload = json.dumps([0, sequence, []]).encode().ljust(evidence.MAX_EVENT_BYTES - evidence.FRAME_BYTES, b' ')
            full.write(struct.pack('>I', len(payload)) + hashlib.sha256(payload).digest() + payload)
        full.seek(0)
        self.assertEqual(len(list(evidence._read_frames(full))), evidence.MAX_BYTES // evidence.MAX_EVENT_BYTES)
        self.assertEqual(full.tell(), evidence.MAX_BYTES)

    def test_sealing_failure_or_deadline_cannot_return_success(self):
        run = self.run_owner()
        original = os.fsync
        calls = 0
        def sync(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError()
            original(fd)
        with patch.object(evidence.os, 'fsync', side_effect=sync):
            with self.assertRaises(evidence.EvidenceFailure):
                run.accept()
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)
        run = self.run_owner()
        with patch.object(evidence.time, 'time', side_effect=[run._deadline - 2, run._deadline - 1, run._deadline + 1, run._deadline + 1]):
            with self.assertRaises(evidence.EvidenceFailure):
                run.accept()
        self.assertEqual(decoded(run.path)[-1][2][0], evidence.Verdict.REJECTED)

    def test_owner_nesting_reentry_and_derived_once(self):
        run = self.run_owner()
        with run.observe(evidence.Stage.LIVE, {}, Timeline()) as observation:
            observation.derived(None)
            with self.assertRaises(evidence.EvidenceFailure):
                observation.derived(None)
            with self.assertRaises(evidence.EvidenceFailure):
                run.observe(evidence.Stage.LIVE, {}, Timeline())

    def test_deferred_observation_has_one_bounded_owner(self):
        run = self.run_owner()
        scope = run.observe(evidence.Stage.READINESS, {}, Timeline())
        with self.assertRaises(evidence.EvidenceFailure):
            run.observe(evidence.Stage.LIVE, {}, Timeline())
        with self.assertRaises(evidence.EvidenceFailure):
            scope.derived(None)
        with scope:
            scope.derived(None)
            with self.assertRaises(evidence.EvidenceFailure):
                run.accept()
        with self.assertRaises(evidence.EvidenceFailure):
            scope.__enter__()
        with patch.object(evidence.os, 'getpid', return_value=-1):
            with self.assertRaises(evidence.EvidenceFailure):
                run.observe(evidence.Stage.LIVE, {}, Timeline())

    def test_wrong_code_pin_refused(self):
        def impostor():
            return None
        with self.assertRaises(evidence.EvidenceFailure):
            evidence._pin(impostor.__code__, Timeline.add.__code__)

    def test_unsupported_windows_private_creation_is_explicit_refusal(self):
        if os.name == 'posix':
            self.skipTest('Windows refusal applies only to Windows')
        with self.assertRaisesRegex(evidence.EvidenceFailure, 'private POSIX'):
            evidence.EvidenceRun.create(Path(self.temporary.name) / 'private', evidence.time.time() + 60,
                                        SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)


class FrozenSourceTests(unittest.TestCase):
    def test_frozen_and_duration_bytes_immutable(self):
        self.assertEqual(hashlib.sha256(FROZEN.read_bytes()).hexdigest(), FROZEN_SHA256)
        self.assertEqual((BASE / 'observation_duration.py').read_bytes(),
                         (BASE.parents[1] / '.audit/startup-r5-ops/observation_duration.py').read_bytes())

    def test_frozen_statements_preserved_except_deferred_summary_publication(self):
        class RemoveEvidence(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return None if node.module == 'startup_evidence' else node

            def visit_Assign(self, node):
                return None if any(isinstance(target, ast.Name) and target.id == 'evidence' for target in node.targets) else self.generic_visit(node)

            def visit_Expr(self, node):
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                    owner = node.value.func.value
                    if isinstance(owner, ast.Name) and owner.id in ('evidence', 'observation'):
                        return None
                return self.generic_visit(node)

            def visit_With(self, node):
                context = node.items[0].context_expr
                if isinstance(context, ast.Call) and isinstance(context.func, ast.Attribute) and context.func.attr == 'observe':
                    return [updated for child in node.body if (updated := self.visit(child)) is not None]
                return self.generic_visit(node)

        stripped = RemoveEvidence().visit(ast.parse(CANDIDATE.read_text(encoding='utf-8')))
        self.assertEqual(ast.dump(stripped.body[-1].value.func), ast.dump(ast.Name(id='print', ctx=ast.Load())))
        self.assertEqual(ast.dump(stripped.body[-2].value.func), ast.dump(ast.Name(id='check_deadline', ctx=ast.Load())))
        stripped.body[-2:] = [stripped.body[-1], stripped.body[-2]]
        self.assertEqual(ast.dump(stripped, include_attributes=False), ast.dump(original_ast, include_attributes=False))
        scopes = [node for node in ast.walk(candidate_ast) if isinstance(node, ast.With)
                  and isinstance(node.items[0].context_expr, ast.Call)
                  and isinstance(node.items[0].context_expr.func, ast.Attribute)
                  and node.items[0].context_expr.func.attr == 'observe']
        self.assertEqual(len(scopes), 3)
        for scope in scopes:
            adds = [node for node in ast.walk(scope) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'add']
            self.assertEqual(len(adds), 1)
            self.assertEqual([ast.dump(arg) for arg in adds[0].args], [ast.dump(ast.Name(id='row', ctx=ast.Load()))])
            self.assertFalse(adds[0].keywords)
        self.assertEqual(ast.dump(next(node for node in candidate_ast.body if isinstance(node, ast.ClassDef) and node.name == 'Timeline')),
                         ast.dump(next(node for node in original_ast.body if isinstance(node, ast.ClassDef) and node.name == 'Timeline')))

    def test_candidate_exact_function_pins(self):
        snapshot = next(node for node in candidate_ast.body if isinstance(node, ast.FunctionDef) and node.name == 'snapshot')
        timeline = next(node for node in candidate_ast.body if isinstance(node, ast.ClassDef) and node.name == 'Timeline')
        add = next(node for node in timeline.body if isinstance(node, ast.FunctionDef) and node.name == 'add')
        self.assertEqual(hashlib.sha256(ast.dump(snapshot, include_attributes=False).encode()).hexdigest(), evidence.SNAPSHOT_AST_SHA256)
        self.assertEqual(hashlib.sha256(ast.dump(add, include_attributes=False).encode()).hexdigest(), evidence.ADD_AST_SHA256)
        evidence._pin(SNAPSHOT['snapshot'].__code__, candidate_timeline.add.__code__)


@unittest.skipUnless(os.name == 'posix', 'Native POSIX directory modes, O_NOFOLLOW, dir_fd and private ACL guarantees unavailable on Windows')
class NativeFilesystemTests(unittest.TestCase):
    setUp = EvidenceTests.setUp

    def native_run(self):
        return evidence.EvidenceRun.create(Path(self.temporary.name) / 'private', evidence.time.time() + 60,
                                           SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)

    def test_native_exclusive_modes_and_collision(self):
        run = self.native_run()
        self.addCleanup(run.finish)
        self.assertEqual(run.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.path.parent.stat().st_mode & 0o777, 0o700)
        prior = run.path.read_bytes()
        with patch.object(evidence.secrets, 'token_hex', return_value=run.path.parent.name):
            with self.assertRaises(evidence.EvidenceFailure):
                self.native_run()
        self.assertEqual(run.path.read_bytes(), prior)
        other = self.native_run()
        self.addCleanup(other.finish)
        self.assertNotEqual(run.path, other.path)

    def test_native_symlink_and_unsafe_parent_refused(self):
        real = Path(self.temporary.name) / 'real'
        real.mkdir(mode=0o700)
        link = Path(self.temporary.name) / 'link'
        link.symlink_to(real, target_is_directory=True)
        for parent in (link, link / 'private'):
            with self.assertRaises(evidence.EvidenceFailure):
                evidence.EvidenceRun.create(parent, evidence.time.time() + 60, SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)
        real.chmod(0o755)
        with self.assertRaises(evidence.EvidenceFailure):
            evidence.EvidenceRun.create(real, evidence.time.time() + 60, SNAPSHOT['snapshot'].__code__, Timeline.add.__code__)

    def test_native_prefix_reader_torn_tail_size_and_symlink(self):
        run = self.native_run()
        with run.observe(evidence.Stage.LIVE, {}, Timeline()) as observation:
            observation.derived(None)
        run.finish()
        intact = run.path.read_bytes()
        self.assertEqual(list(evidence.read_prefix(run.path)), decoded(run.path))
        for cut in (1, 10, evidence.FRAME_BYTES, 50):
            path = Path(self.temporary.name) / ('torn' + str(cut))
            path.write_bytes(intact[:-cut])
            prior = path.read_bytes()
            self.assertLess(len(list(evidence.read_prefix(path))), 4)
            self.assertEqual(path.read_bytes(), prior)
        link = Path(self.temporary.name) / 'journal-link'
        link.symlink_to(run.path)
        with self.assertRaises(OSError):
            list(evidence.read_prefix(link))
        large = Path(self.temporary.name) / 'large'
        with large.open('wb') as stream:
            stream.truncate(evidence.MAX_BYTES + 1)
        with self.assertRaises(evidence.EvidenceFailure):
            list(evidence.read_prefix(large))

    def test_native_descriptor_closed_after_before_leaves_incomplete_prefix(self):
        run = self.native_run()
        run.observe(evidence.Stage.READINESS, status_at(), Timeline()).__enter__()
        os.close(run._fd)
        run._closed = True
        events = list(evidence.read_prefix(run.path))
        self.assertEqual([event[0] for event in events], [evidence.Event.OPEN, evidence.Event.BEFORE])
        self.assertFalse(any(event[0] == evidence.Event.TERMINAL for event in events))


if __name__ == '__main__':
    unittest.main()

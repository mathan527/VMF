"""HostSampler.summary() and measure(host_load_out=...): the host-load numbers that go into
perf_meta.json. No device, no real psutil.
"""
import logging

import pytest
from test_perf import FakePsutil, FakeSampler, run_measure, steady_adb

from vmf.perf import HostSampler


def manual_sampler(system_values, procs=(("qemu-system-x86_64.exe", 100),), cores=4, ram=8_000_000_000):
    """A sampler fed by hand (no thread) with one reading per value in system_values."""
    fake = FakePsutil(system_values[0], list(procs), cores=cores)
    sampler = HostSampler(fake, interval_s=3600, noisy_threshold=30.0)
    for value in system_values:
        fake.system = value
        sampler._sample_once()
    return sampler


def test_summary_reports_min_mean_max_for_the_whole_machine_and_outside_the_emulator():
    s = manual_sampler([40, 60, 80])                       # emulator takes 100/4 = 25 points each time
    summary = s.summary()
    assert summary["samples"] == 3
    assert summary["cpu_pct"] == {"min": 40.0, "mean": pytest.approx(60.0), "max": 80.0}
    assert summary["cpu_outside_emulator_pct"] == {"min": 15.0, "mean": pytest.approx(35.0), "max": 55.0}
    assert summary["free_ram_min_bytes"] == 8_000_000_000
    assert summary["noisy_threshold_pct"] == 30.0


def test_summary_keeps_the_lowest_free_ram_seen():
    fake = FakePsutil(10, [])
    sampler = HostSampler(fake, interval_s=3600)
    for free in (9_000_000_000, 3_000_000_000, 7_000_000_000):
        fake.virtual_memory = lambda free=free: type("M", (), {"available": free})()
        sampler._sample_once()
    assert sampler.summary()["free_ram_min_bytes"] == 3_000_000_000


def test_summary_with_no_samples_is_empty_not_an_error():
    summary = HostSampler(FakePsutil(1, []), interval_s=3600).summary()
    assert summary["samples"] == 0 and summary["cpu_pct"] is None
    assert summary["cpu_outside_emulator_pct"] is None and summary["free_ram_min_bytes"] is None


def test_summary_is_consistent_with_the_noisy_flag():
    s = manual_sampler([80, 80, 80])
    assert s.noisy is True and s.summary()["cpu_outside_emulator_pct"]["mean"] == pytest.approx(55.0)


class SummarySampler(FakeSampler):
    def summary(self):
        return {"samples": 3, "cpu_pct": {"min": 1.0, "mean": 2.0, "max": 3.0},
                "cpu_outside_emulator_pct": None, "free_ram_min_bytes": 5, "noisy_threshold_pct": 30.0}


def test_measure_fills_host_load_out_from_the_sampler():
    out = {}
    run_measure(steady_adb(), sampler_factory=SummarySampler, host_load_out=out)
    assert out["samples"] == 3 and out["cpu_pct"]["max"] == 3.0 and out["free_ram_min_bytes"] == 5


def test_measure_works_without_host_load_out_and_leaves_it_alone_when_the_sampler_has_no_summary():
    rows = run_measure(steady_adb())
    assert len(rows) == 3
    out = {}
    run_measure(steady_adb(), sampler_factory=FakeSampler, host_load_out=out)    # FakeSampler has no summary()
    assert out == {}


def test_a_failing_summary_is_logged_with_a_traceback_and_the_rows_are_still_returned(caplog):
    class Broken(FakeSampler):
        def summary(self):
            raise RuntimeError("summary broke")
    out = {}
    with caplog.at_level(logging.ERROR):
        rows = run_measure(steady_adb(), sampler_factory=Broken, host_load_out=out)
    assert len(rows) == 3 and out == {}
    assert any(r.exc_info and "summary broke" in str(r.exc_info[1]) for r in caplog.records)

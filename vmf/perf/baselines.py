"""baselines.py — performance baselines and their promotion (workbook M8.9).

A baseline is the accepted median of a metric for one profile; a later run is
compared against it (regression_pct). Every number is EMULATOR-RELATIVE.

* ``BaselineStore.load / load_all`` read baselines (read-only).
* ``BaselineStore.promote`` is the ONLY way to create or change one, and nothing
  calls it automatically: no run, no test and no code path promotes unless
  somebody calls it (or the ``python -m vmf.perf promote`` command) with an
  ``approved_by`` name that is listed under ``baseline_approvers`` in
  perf_config.yaml. Only the lead is meant to be in that list.

Two stores sit behind :func:`get_store`:

* ``StorageBaselineStore`` uses ``storage.load_baselines`` / ``storage.save_baseline``
  when vmf.storage has them. They were requested from Gagan and do not exist yet;
  the assumed contract is ``load_baselines(profile) -> {metric: float}`` and
  ``save_baseline(profile, metric, value, *, build_sha256, promoted_by, run_id)``.
  This module never touches sqlite.
* ``YamlBaselineStore`` (the interim default) keeps ``perf_baselines.yaml`` at the
  repo root, written atomically.

Profile names are sanitised the same way as everywhere else in the orchestrator
(see :func:`sanitise_profile`), so a baseline for "Samsung A07" is keyed
"Samsung_A07".
"""
import datetime
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from vmf import storage
from vmf.perf import perf

log = logging.getLogger(__name__)

BASELINES_FILE_NAME = "perf_baselines.yaml"
_HEADER = ("# perf_baselines.yaml - accepted EMULATOR-RELATIVE baselines, one per profile and metric.\n"
           "# Written only by `python -m vmf.perf promote` (an approver listed in perf_config.yaml).\n"
           "# Do not edit by hand.\n")


def sanitise_profile(name: str) -> str:
    """The profile key used for evidence folders, storage and baselines: anything that
    is not a letter, digit, ``_``, ``.`` or ``-`` (spaces included) becomes ``_``."""
    return re.sub(r"[^\w.-]+", "_", str(name).strip())


class BaselineError(ValueError):
    """A baseline operation was refused or could not be done."""


class PromotionRefused(BaselineError):
    """Promotion refused; ``reasons`` lists every profile/metric that blocked it."""

    def __init__(self, reasons: list[str]):
        super().__init__("promotion refused: " + "; ".join(reasons))
        self.reasons = reasons


class BaselineStore(ABC):
    """Interface of a baseline store."""

    name = "baseline store"

    @abstractmethod
    def load(self, profile: str) -> dict:
        """``{metric: float}`` for one profile ({} if it has no baseline)."""

    @abstractmethod
    def _write(self, updates: list[dict]) -> None:
        """Persist promoted values. Each update has profile, metric, value,
        build_sha256, promoted_by, promoted_on, run_id."""

    @abstractmethod
    def all_entries(self, profile_names=None) -> list[dict]:
        """Every stored baseline with its provenance (for the listing command)."""

    def load_all(self, profile_names) -> dict:
        """``{profile: {metric: float}}`` for the profiles that HAVE a baseline."""
        found = {}
        for name in profile_names:
            values = self.load(name)
            if values:
                found[name] = values
        return found

    def promote(self, run_id: int, rows_by_profile: dict, approved_by: str, *,
                allow_overwrite: bool = False, builds: dict | None = None,
                approvers: list | None = None, now: datetime.datetime | None = None) -> list[tuple]:
        """Make the medians of ``rows_by_profile`` ({profile: [PerfRow]}) the baselines.

        Refused (nothing written, :class:`BaselineError` / :class:`PromotionRefused`) unless:

        * ``approvers`` (default: ``baseline_approvers`` of perf_config.yaml) is not
          empty, and ``approved_by`` is one of them;
        * every row is neither noisy nor unstable;
        * a profile that already has a baseline (for any metric) is only changed with
          ``allow_overwrite=True``; the old and new values are returned and logged.

        ``builds`` maps profile to the APK sha256 recorded with the baseline. Returns
        ``[(profile, metric, old, new)]`` (``old`` None if there was none).
        """
        allowed = perf.load_approvers() if approvers is None else list(approvers)
        if not allowed:
            raise BaselineError("no baseline approvers are configured: add the lead's name to "
                                "'baseline_approvers' in perf_config.yaml before promoting")
        if not approved_by or approved_by.strip().casefold() not in {a.strip().casefold() for a in allowed}:
            raise BaselineError(f"{approved_by!r} is not in baseline_approvers "
                                f"({', '.join(allowed)}); only an approver can promote a baseline")
        if not rows_by_profile:
            raise BaselineError("nothing to promote: no perf rows were given")
        when = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="seconds")
        refusals, updates, promoted = [], [], []
        for profile, rows in rows_by_profile.items():
            key = sanitise_profile(profile)
            existing = self.load(key)
            if existing and not allow_overwrite:   # the profile has a baseline: never replaced silently
                refusals.append(f"{key} already has a baseline ({', '.join(f'{m}={v}' for m, v in sorted(existing.items()))}); "
                                f"pass allow_overwrite=True to replace it")
            for row in rows:
                label = f"{key}/{row.metric}"
                if row.noisy:
                    refusals.append(f"{label} was measured on a noisy host")
                if row.unstable:
                    refusals.append(f"{label} was unstable (spread {row.spread_pct:.1f}%)")
                updates.append({"profile": key, "metric": row.metric, "value": float(row.median),
                                "build_sha256": (builds or {}).get(profile), "promoted_by": approved_by.strip(),
                                "promoted_on": when, "run_id": run_id})
                promoted.append((key, row.metric, existing.get(row.metric), float(row.median)))
        if refusals:
            raise PromotionRefused(refusals)
        self._write(updates)
        log.warning("baselines promoted by %s from run %s in %s: %s", approved_by, run_id, self.name,
                    ", ".join(f"{p}/{m}: {o} -> {n}" for p, m, o, n in promoted))
        return promoted


class YamlBaselineStore(BaselineStore):
    """Baselines in ``perf_baselines.yaml`` (repo root by default)."""

    name = "perf_baselines.yaml"

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else perf.REPO_ROOT / BASELINES_FILE_NAME

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            log.exception("baselines: %s is not valid YAML", self.path)
            raise BaselineError(f"{self.path} is not valid YAML: {e}") from e
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict):
            raise BaselineError(f"{self.path} must have a 'profiles' mapping")
        return profiles

    def load(self, profile: str) -> dict:
        entries = self._read().get(sanitise_profile(profile), {})
        return {metric: float(e["value"]) for metric, e in entries.items()}

    def all_entries(self, profile_names=None) -> list[dict]:
        out = []
        for profile, entries in sorted(self._read().items()):
            for metric, e in sorted(entries.items()):
                out.append({"profile": profile, "metric": metric, **e})
        return out

    def _write(self, updates: list[dict]) -> None:
        profiles = self._read()
        for u in updates:
            profiles.setdefault(u["profile"], {})[u["metric"]] = {
                "value": u["value"], "build_sha256": u["build_sha256"],
                "promoted_by": u["promoted_by"], "promoted_on": u["promoted_on"], "run_id": u["run_id"]}
        self._atomic_write({"profiles": profiles})

    def _atomic_write(self, data: dict) -> None:
        """Write to a temp file in the same folder, then ``os.replace`` it over the real
        one, so a failure at any point leaves the old file intact."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_HEADER)
                yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            log.exception("could not write %s; the old file is unchanged", self.path)
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                log.warning("baselines: temp file %s was already gone", tmp, exc_info=True)
            raise


class StorageBaselineStore(BaselineStore):
    """Baselines kept by vmf.storage (only when it provides load_baselines/save_baseline)."""

    name = "storage"

    def __init__(self, storage_module):
        self._storage = storage_module

    def load(self, profile: str) -> dict:
        return {m: float(v) for m, v in dict(self._storage.load_baselines(sanitise_profile(profile))).items()}

    def all_entries(self, profile_names=None) -> list[dict]:
        if hasattr(self._storage, "list_baseline_entries"):
            return self._storage.list_baseline_entries(profile_names)
        out = []
        for profile in profile_names or []:
            for metric, value in sorted(self.load(profile).items()):
                out.append({"profile": sanitise_profile(profile), "metric": metric, "value": value})
        return out

    def _write(self, updates: list[dict]) -> None:
        for u in updates:
            self._storage.save_baseline(u["profile"], u["metric"], u["value"],
                                        build_sha256=u["build_sha256"], promoted_by=u["promoted_by"],
                                        run_id=u["run_id"])


def get_store(storage_module=None, yaml_path: str | os.PathLike | None = None) -> BaselineStore:
    """The storage-backed store if vmf.storage has ``load_baselines`` and ``save_baseline``,
    else the YAML file store. Logs which one is in use."""
    mod = storage if storage_module is None else storage_module
    if hasattr(mod, "load_baselines") and hasattr(mod, "save_baseline"):
        log.info("baselines: using the storage functions load_baselines/save_baseline")
        return StorageBaselineStore(mod)
    store = YamlBaselineStore(yaml_path)
    log.info("baselines: vmf.storage has no load_baselines/save_baseline yet; using %s", store.path)
    return store

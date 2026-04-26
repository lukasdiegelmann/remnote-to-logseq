from __future__ import annotations
import sys
import time


def _fmt_dur(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Progress:
    def __init__(self, total: int, label: str, width: int = 30):
        self.total = max(total, 0)
        self.label = label
        self.width = width
        self.n = 0
        self.start = time.monotonic()
        self.last_print = 0.0
        self.enabled = self.total > 0
        self.tty = sys.stderr.isatty()

    def tick(self, by: int = 1, suffix: str = "") -> None:
        self.n += by
        if not self.enabled:
            return
        now = time.monotonic()
        min_gap = 0.1 if self.tty else 5.0
        if now - self.last_print < min_gap and self.n < self.total:
            return
        self.last_print = now
        elapsed = now - self.start
        frac = self.n / self.total if self.total else 1.0
        filled = int(self.width * frac)
        bar = "#" * filled + "-" * (self.width - filled)
        eta_s = _fmt_dur(elapsed / self.n * (self.total - self.n)) if self.n else "?"
        msg = (f"{self.label} [{bar}] {self.n}/{self.total} "
               f"elapsed {_fmt_dur(elapsed)} ETA {eta_s}")
        if suffix:
            msg += f" {suffix[:40]}"
        if self.tty:
            sys.stderr.write("\r" + msg.ljust(120)[:120])
        else:
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def done(self) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self.start
        line = f"{self.label}: {self.n}/{self.total} done in {_fmt_dur(elapsed)}"
        if self.tty:
            sys.stderr.write("\r" + line.ljust(120)[:120] + "\n")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

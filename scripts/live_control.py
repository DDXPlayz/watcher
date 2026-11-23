"""Manual live check of the PVR adapter. Not part of the test suite; each run loads a few real pages.

  python -m scripts.live_control target     # the configured target
  python -m scripts.live_control control URL "FILM" LANGUAGE YYYY-MM-DD   # a film known to be in ScreenX at Jasola

The control proves the detector can say yes. Without one, 'nothing found' could just mean 'broken'.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from app.config import load_config
from app.domain import Target
from app.monitor.matcher import Matcher
from app.platforms.pvr import PvrPlatform


async def main(argv: list[str]) -> None:
    cfg = load_config()
    target = cfg.target
    pvr_cfg, aliases = cfg.platforms.pvr, cfg.aliases
    if argv and argv[0] == "control":
        url, film, language, date = argv[1:5]
        target = Target.model_validate({**target.model_dump(), "movie": film, "language": language, "date": date})
        pvr_cfg = pvr_cfg.model_copy(update={"movie_url": url})
        aliases = aliases.model_copy(update={"movie": [film]})
    platform = PvrPlatform(pvr_cfg, aliases)
    try:
        for _ in range(2):  # the second run shows the steady-state duration
            started = time.monotonic()
            result = await platform.check_availability(target)
            print(f"{time.monotonic() - started:.1f}s", result.state, result.error_kind, result.error_detail)
        print(json.dumps(result.signals, ensure_ascii=False))
        matcher = Matcher(target, aliases)
        for obs in result.observations:
            decision = matcher.evaluate(obs)
            if decision.state:
                print(decision.state, obs.showtime, obs.format, obs.booking_url, obs.seats.available, obs.seats.total, obs.evidence)
                print("  ", [(c.name, c.available, c.price_text) for c in obs.seats.categories], obs.seats.available_seats[:8])
    finally:
        await platform.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

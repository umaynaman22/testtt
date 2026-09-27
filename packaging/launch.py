"""Entry point for the Windows build: RentalTracker.exe runs the normal launcher."""
import multiprocessing

from rental_tracker.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

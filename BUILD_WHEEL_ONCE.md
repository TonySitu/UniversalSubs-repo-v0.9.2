# One-time: build the proctap wheel for distribution (developer machine only)

Users of UniversalSubs should never need a compiler. You build the wheel once
and ship it in a `wheels/` folder next to the installer.

1. Install Microsoft C++ Build Tools (one-time):
   https://visualstudio.microsoft.com/visual-cpp-build-tools/
   -> check the "Desktop development with C++" workload -> install -> reboot

2. In your project folder, run:
   mkdir wheels
   python -m pip wheel https://github.com/m96-chan/ProcTap/archive/refs/heads/main.zip --no-deps -w wheels

3. You should now have something like:
   wheels\proc_tap-0.4.x-cp311-cp311-win_amd64.whl

4. Commit the `wheels/` folder to your repo. install_and_run.bat installs
   from it automatically (no compiler, no internet needed for that step).

NOTE: a wheel is tied to a Python version — cp311 = Python 3.11. If your
users run a different Python, build additional wheels with that Python, or
(the real endgame) package the whole app with PyInstaller so users install
nothing at all.

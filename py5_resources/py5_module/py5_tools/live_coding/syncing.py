# *****************************************************************************
#
#   Part of the py5 library
#   Copyright (C) 2020-2024 Jim Schmitz
#
#   This library is free software: you can redistribute it and/or modify it
#   under the terms of the GNU Lesser General Public License as published by
#   the Free Software Foundation, either version 2.1 of the License, or (at
#   your option) any later version.
#
#   This library is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser
#   General Public License for more details.
#
#   You should have received a copy of the GNU Lesser General Public License
#   along with this library. If not, see <https://www.gnu.org/licenses/>.
#
# *****************************************************************************
import datetime as dt
import inspect
import os
import sys
import zipfile
from pathlib import Path

import stackprinter

from .import_hook import activate_py5_live_coding_import_hook

LIVE_CODING_FILE = 1
LIVE_CODING_GLOBALS = 2


######################################################################
# HELPER FUNCTIONS
######################################################################


STARTUP_CODE = """
__name__ = "__main__"
__doc__ = None
__package__ = None
__spec__ = None
__annotations__ = dict()
__file__ = "{0}"
__cached__ = None
"""


def init_namespace(filename, global_namespace):
    global_namespace.clear()
    exec(STARTUP_CODE.format(Path(filename).absolute()), global_namespace)


def is_subdirectory(d, f):
    d = Path(d).resolve()
    f = Path(f).resolve()
    return f.parts[: len(d.parts)] == d.parts


class Py5RunSketchBlockException(Exception):
    pass


######################################################################
# USER FUNCTION WRAPPERS
######################################################################


class UserFunctionWrapper:
    exception_state = False

    def __new__(self, sketch, name, f, param_count):
        ufw = object.__new__(
            UserFunctionWrapperOneParam
            if param_count == 1
            else UserFunctionWrapperZeroParams
        )
        ufw.sketch = sketch
        ufw.name = name
        ufw.f = f
        return ufw

    def call_f(self, *args):
        import py5.bridge as py5_bridge

        try:
            if self.f is not None and not UserFunctionWrapper.exception_state:
                self.f(*args)
        except Exception as e:
            UserFunctionWrapper.exception_state = True
            self.sketch.no_loop()
            self.sketch.println("*" * 80)
            py5_bridge.handle_exception(self.sketch.println, *sys.exc_info())
            self.sketch.println("*" * 80)

            # if we are in file mode, watch code for changes that will fix the
            # problem if we aren't doing so already
            if (
                self.sketch._get_sync_draw().live_coding_mode & LIVE_CODING_FILE
                and not self.sketch.has_thread("keep_functions_current_from_file")
            ):
                self.sketch.launch_repeating_thread(
                    self.sketch._get_sync_draw().keep_functions_current_from_file,
                    "keep_functions_current_from_file",
                    time_delay=0.01,
                    args=(self.sketch,),
                )


"""
These two subclasses need to be here because py5 will inspect user functions to
determine the paremeter counts. I can't just remove these and use the above
class with a `__call__(self, *args)` method because the `*args` parameter will
trip up py5.
"""


class UserFunctionWrapperZeroParams(UserFunctionWrapper):
    def __call__(self):
        self.call_f()


class UserFunctionWrapperOneParam(UserFunctionWrapper):
    def __call__(self, arg):
        self.call_f(arg)


######################################################################
# CODE PROCESSING FUNCTIONS
######################################################################


def exec_user_code(
    sketch, filename, global_namespace, mock_run_sketch, activate_keyboard_shortcuts
):
    # get user functions by executing code in the given filename, for LIVE_CODING_FILE mode
    import py5.bridge as py5_bridge

    init_namespace(filename, global_namespace)

    # execute user code and put new functions into the global namespace

    try:
        with open(filename, "r") as f:
            exec(compile(f.read(), filename=filename, mode="exec"), global_namespace)
    except Py5RunSketchBlockException:
        # MockRunSketch instance has replaced run_sketch() in the py5 module
        functions, function_param_counts = (
            mock_run_sketch._functions,
            mock_run_sketch._function_param_counts,
        )
    else:
        # the user didn't call run_sketch() in their code. will issue a warning later
        functions, function_param_counts = py5_bridge._extract_py5_user_function_data(
            global_namespace
        )

    return process_user_functions(
        sketch,
        functions,
        function_param_counts,
        global_namespace,
        activate_keyboard_shortcuts,
    )


def retrieve_user_code(sketch, namespace, activate_keyboard_shortcuts):
    # get user functions from the given namespace, for LIVE_CODING_GLOBALS mode
    import py5.bridge as py5_bridge

    functions, function_param_counts = py5_bridge._extract_py5_user_function_data(
        namespace
    )

    return process_user_functions(
        sketch, functions, function_param_counts, namespace, activate_keyboard_shortcuts
    )


def process_user_functions(
    sketch, functions, function_param_counts, namespace, activate_keyboard_shortcuts
):
    # process the user functions, adding any missing functions and wrapping them
    # used by both LIVE_CODING_FILE and LIVE_CODING_GLOBALS modes
    from py5 import _split_setup as py5_split_setup

    functions = (
        py5_split_setup.transform(
            functions, namespace, namespace, sketch.println, mode="module"
        )
        or {}
    )

    user_supplied_draw = "draw" in functions

    for fname in ["settings", "setup", "draw", "key_typed"]:
        # the key_typed one is only needed if activate_keyboard_shortcuts is True
        if fname == "key_typed" and not activate_keyboard_shortcuts:
            continue

        if fname not in functions:
            functions[fname] = lambda: None
            function_param_counts[fname] = 0

    functions = {
        name: UserFunctionWrapper(sketch, name, f, function_param_counts.get(name, 0))
        for name, f in functions.items()
    }

    return functions, function_param_counts, user_supplied_draw


######################################################################
# SYNC DRAW CLASS
######################################################################


class SyncDraw:
    def __init__(
        self,
        live_coding_mode,
        *,
        filename=None,
        global_namespace=None,
        always_rerun_setup=False,
        always_on_top=True,
        show_framerate=False,
        activate_keyboard_shortcuts=False,
        watch_dir=False,
        archive_dir=None,
        mock_run_sketch=None,
    ):
        self.live_coding_mode = live_coding_mode

        self.filename = Path(filename) if live_coding_mode & LIVE_CODING_FILE else None
        self.global_namespace = global_namespace

        self.always_rerun_setup = always_rerun_setup
        self.always_on_top = always_on_top
        self.show_framerate = show_framerate
        self.activate_keyboard_shortcuts = activate_keyboard_shortcuts
        self.watch_dir = watch_dir
        self.archive_dir = Path(archive_dir)
        self.mock_run_sketch = mock_run_sketch

        if self.watch_dir:
            self.getmtime = lambda f: max(
                (
                    os.path.getmtime(ff)
                    for ff in f.parent.glob("**/*")
                    if ff.is_file()
                    and not is_subdirectory(self.archive_dir, ff)
                    and not ff.suffix == ".pyc"
                ),
                default=0,
            )
            self.import_hook = activate_py5_live_coding_import_hook(
                self.filename.parent.absolute()
            )
        else:
            self.getmtime = os.path.getmtime
            self.import_hook = None

        self.startup = True
        self.update_count = 0
        self.mtime = None
        self.capture_pixels = None
        self.functions = {}
        self.function_param_counts = {}
        self.user_supplied_draw = False
        self.user_setup_code = None
        self.run_setup_again = False

    ######################################################################
    # HOOK METHODS
    ######################################################################

    def _init_hooks(self, s):
        s._set_sync_draw(self)

        s._add_pre_hook("setup", "sync_pre_setup", self.pre_setup_hook)
        s._add_pre_hook("draw", "sync_pre_draw", self.pre_draw_hook)
        if self.activate_keyboard_shortcuts:
            s._add_pre_hook("key_typed", "sync_pre_key_typed", self.pre_key_typed_hook)
        s._add_post_hook("setup", "sync_post_setup", self.post_setup_hook)
        s._add_post_hook("draw", "sync_post_draw", self.post_draw_hook)

    def pre_setup_hook(self, s):
        if self.always_on_top:
            s.get_surface().set_always_on_top(True)

    def post_setup_hook(self, s):
        if self.always_on_top:
            self.capture_pixels = s.get_pixels()

    def pre_draw_hook(self, s):
        if self.run_setup_again:
            s._instance._resetSyncSketch()
            # in case user doesn't call background in setup
            s.background(204)
            self.functions["setup"]()
            self.run_setup_again = False

        if self.capture_pixels is not None:
            s.set_pixels(0, 0, self.capture_pixels)
            self.capture_pixels = None

    def post_draw_hook(self, s):
        if (
            self.show_framerate
            and self.user_supplied_draw
            and not UserFunctionWrapper.exception_state
        ):
            msg = f"frame rate: {s.get_frame_rate():0.1f}"
            s.println(msg, end="\r")

        if self.live_coding_mode & LIVE_CODING_FILE:
            self.keep_functions_current_from_file(s)

    def pre_key_typed_hook(self, s):
        datestr = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{self.filename.stem}_{datestr}"

        if s.key == "R":
            self.run_setup_again = True
        if s.key in "AS":
            self.take_screenshot(s, name)
        if s.key in "AC":
            self.copy_code(s, name)

    ######################################################################
    # ARCHIVE METHODS
    ######################################################################

    def get_filename_stem(self):
        return self.filename.stem if self.filename else None

    def take_screenshot(self, s, screenshot_name: str):
        if UserFunctionWrapper.exception_state:
            s.println(f"Skipping screenshot due to error state")
            return

        self.archive_dir.mkdir(exist_ok=True)
        screenshot_filename = self.archive_dir / screenshot_name

        if not screenshot_filename.suffix:
            screenshot_filename = screenshot_filename.with_suffix(".png")

        if screenshot_filename.exists():
            s.println(
                f"Skipping screenshot because {screenshot_filename} already exists"
            )
            return

        s.save_frame(screenshot_filename)
        s.println(f"Screenshot saved to {screenshot_filename}")

    def copy_code(self, s, copy_name: str):
        if self.live_coding_mode & LIVE_CODING_GLOBALS:
            s.println(f"Skipping code copying because code is not in a *.py file")
            return

        if UserFunctionWrapper.exception_state:
            s.println(f"Skipping code copying due to error state")
            return

        self.archive_dir.mkdir(exist_ok=True)
        copy_filename = self.archive_dir / copy_name

        if self.watch_dir:
            copy_filename = copy_filename.with_suffix(".zip")
            if copy_filename.exists():
                s.println(
                    f"Skipping code copying because {copy_filename} already exists"
                )
                return

            with zipfile.ZipFile(copy_filename, "w", zipfile.ZIP_DEFLATED) as zf:
                for ff in self.filename.parent.glob("**/*"):
                    if (
                        ff.is_file()
                        and not is_subdirectory(self.archive_dir, ff)
                        and not ff.suffix == ".pyc"
                    ):
                        zf.write(ff, ff.relative_to(self.filename.parent))
        else:
            copy_filename = copy_filename.with_suffix(".py")
            if copy_filename.exists():
                s.println(
                    f"Skipping code copying because {copy_filename} already exists"
                )
                return

            with open(self.filename, "r") as f:
                with open(copy_filename, "w") as f2:
                    f2.write(f.read())

        s.println(f"Code copied to {copy_filename}")

    ######################################################################
    # CODE PROCESSING METHODS
    ######################################################################

    def keep_functions_current_from_globals(self, s):
        try:
            self.functions, self.function_param_counts, self.user_supplied_draw = (
                retrieve_user_code(
                    s, self.global_namespace, self.activate_keyboard_shortcuts
                )
            )

            self._process_new_functions(s)

            if UserFunctionWrapper.exception_state:
                s.println("Resuming Sketch execution...")
                UserFunctionWrapper.exception_state = False
                s.loop()

            return True

        except Exception as e:
            UserFunctionWrapper.exception_state = True
            exc_type, exc_value, exc_tb = sys.exc_info()
            exc_tb = exc_tb.tb_next.tb_next
            msg = "*" * 80 + "\n"
            msg += stackprinter.format(
                thing=(exc_type, exc_value, exc_tb),
                show_vals="line",
                style="plaintext",
                suppressed_paths=[
                    r"lib/python.*?/site-packages/numpy/",
                    r"lib/python.*?/site-packages/py5/",
                    r"lib/python.*?/site-packages/py5tools/",
                ],
            )
            msg += "\n" + "*" * 80
            s.println(msg)

            return False

    def keep_functions_current_from_file(self, s, force_update=False):
        try:
            if (
                self.mtime != (new_mtime := self.getmtime(self.filename))
                or force_update
            ):
                self.mtime = new_mtime

                if self.import_hook is not None:
                    self.import_hook.flush_imported_modules()

                self.functions, self.function_param_counts, self.user_supplied_draw = (
                    exec_user_code(
                        s,
                        self.filename,
                        self.global_namespace,
                        self.mock_run_sketch,
                        self.activate_keyboard_shortcuts,
                    )
                )

                self._process_new_functions(s)

                if UserFunctionWrapper.exception_state:
                    if s.has_thread("keep_functions_current_from_file"):
                        s.stop_thread("keep_functions_current_from_file")

                    s.println("Resuming Sketch execution...")
                    UserFunctionWrapper.exception_state = False
                    s.loop()

            return True

        except Exception as e:
            UserFunctionWrapper.exception_state = True
            exc_type, exc_value, exc_tb = sys.exc_info()
            exc_tb = exc_tb.tb_next.tb_next
            msg = "*" * 80 + "\n"
            msg += stackprinter.format(
                thing=(exc_type, exc_value, exc_tb),
                show_vals="line",
                style="plaintext",
                suppressed_paths=[
                    r"lib/python.*?/site-packages/numpy/",
                    r"lib/python.*?/site-packages/py5/",
                    r"lib/python.*?/site-packages/py5tools/",
                ],
            )
            msg += "\n" + "*" * 80
            s.println(msg)

            # watch code for changes that will fix the problem and let Sketch continue
            if not s.has_thread("keep_functions_current_from_file"):
                s.launch_repeating_thread(
                    self.keep_functions_current_from_file,
                    "keep_functions_current_from_file",
                    time_delay=0.01,
                    args=(s,),
                )

            return False

    def _process_new_functions(self, s):
        self.update_count += 1

        new_user_setup_code = (
            inspect.getsource(self.functions["setup"].f)
            if "setup" in self.functions
            else None
        )

        if self.startup:
            self.user_setup_code = new_user_setup_code
            self.startup = False
        else:
            s._py5_bridge.set_functions(self.functions, self.function_param_counts)
            s._instance.buildPy5Bridge(
                s._py5_bridge,
                s._environ.in_ipython_session,
                s._environ.in_jupyter_zmq_shell,
            )

            if self.always_rerun_setup or self.user_setup_code != new_user_setup_code:
                self.run_setup_again = True

            self.user_setup_code = new_user_setup_code

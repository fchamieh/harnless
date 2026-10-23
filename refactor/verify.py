"""What a checkout must be able to do before anything else is asked of it."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import harnless
from harnless_core import agent

print("entry point:", harnless.__file__)
print("loop lives in:", agent.run_agent.__module__)
print("executor present:", harnless.execute_tool.__name__, "dispatch:", len(harnless.DISPATCH), "tools")
print("python:", sys.version.split()[0], "platform:", harnless.os.name)

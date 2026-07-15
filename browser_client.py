# Backward compatibility shim
from . import browser_client as _impl
from . import _shared
import sys

# Re-export everything the old module exposed
sys.modules[__name__] = _impl

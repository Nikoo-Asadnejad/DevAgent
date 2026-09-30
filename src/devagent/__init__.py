import os

# openhands-sdk imports LiteLLM, which otherwise downloads a model price map on import (slow, and it blocks for
# minutes on hosts without outbound access). The bundled map is sufficient for ACP cost estimates.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

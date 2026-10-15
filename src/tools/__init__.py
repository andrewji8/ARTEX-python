"""ARTEX Tools — LLM Gateway, Traffic Proxy, Notification Dispatcher."""

from src.tools.real_tools import ToolExecutor, ToolPlan
from src.tools.traffic_proxy import TrafficCaptureAddon, start_proxy
from src.tools.notifier import (
    ChannelType,
    EmailChannel,
    NotificationDispatcher,
    NotificationMessage,
    RateLimiter,
    get_dispatcher,
    reset_dispatcher,
)

# LLM Gateway — lazy import (requires tenacity + httpx)
try:
    from src.tools.llm_gateway import (
        CircuitBreaker,
        EndpointConfig,
        LLMCallResult,
        LLMPool,
        LLMProvider,
        ProviderType,
        TokenUsage,
        create_anthropic_pool,
        create_openai_pool,
    )
    __all__ = [
        # Real tools (Route B)
        "ToolExecutor",
        "ToolPlan",
        # LLM Gateway
        "LLMPool",
        "LLMProvider",
        "CircuitBreaker",
        "EndpointConfig",
        "LLMCallResult",
        "TokenUsage",
        "ProviderType",
        "create_openai_pool",
        "create_anthropic_pool",
        # Traffic Proxy
        "TrafficCaptureAddon",
        "start_proxy",
        "sanitize_headers",
        # Notifier
        "NotificationDispatcher",
        "NotificationMessage",
        "ChannelType",
        "RateLimiter",
        "get_dispatcher",
        "reset_dispatcher",
    ]
except ImportError:
    __all__ = [
        # Real tools (Route B)
        "ToolExecutor",
        "ToolPlan",
        # Traffic Proxy
        "TrafficCaptureAddon",
        "start_proxy",
        "sanitize_headers",
        # Notifier
        "NotificationDispatcher",
        "NotificationMessage",
        "ChannelType",
        "RateLimiter",
        "get_dispatcher",
        "reset_dispatcher",
    ]

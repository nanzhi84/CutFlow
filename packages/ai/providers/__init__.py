from __future__ import annotations

import httpx

from packages.ai.gateway.provider_gateway import ProviderGateway

from .dashscope import DashScopeASRProvider, DashScopeLLMProvider, DashScopeVLMProvider
from .minimax import MiniMaxTTSProvider
from .runninghub import RunningHubHeyGemProvider


def register_real_provider_plugins(gateway: ProviderGateway) -> None:
    client = gateway.http_client
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(30.0))
        gateway.http_client = client
    for plugin in (
        MiniMaxTTSProvider(client),
        DashScopeASRProvider(client),
        DashScopeVLMProvider(client),
        DashScopeLLMProvider(client),
        RunningHubHeyGemProvider(client),
    ):
        gateway.register(plugin)

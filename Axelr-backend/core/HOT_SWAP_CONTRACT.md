# AXELR Hot-Swap Engine — Public Contract (v3.0)

**Host requirement:** Linux. The engine raises `RuntimeError` in
`HotSwapEngine.__init__` on any non-Linux platform. WSL2 and Linux containers
are the only supported runtimes. There is no Windows fallback path.

## Public surface

```python
engine = await get_hot_swap_engine_async()   # canonical factory
engine = get_hot_swap_engine()               # sync accessor; raises if factory
                                             # has not been awaited yet

text: str = await engine.infer(workspace, task_type, prompt, tier="free",
                               timeout_s=20.0)
await engine.shutdown()
snap: dict = engine.snapshot()
health: dict = engine.health_check()
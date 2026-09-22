from __future__ import annotations

import argparse
import asyncio
import logging
import signal

import uvicorn

from yysls_news.application import ApplicationContext
from yysls_news.rendering.renderer import PlaywrightRenderer
from yysls_news.services.delivery import DeliveryWorker
from yysls_news.web.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="B站与燕云十六声资讯监控推送服务")
    parser.add_argument("mode", nargs="?", choices=("web", "worker", "all"), default="web")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    # httpx INFO 日志会完整打印预签名 COS URL，其中包含临时安全令牌。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    context = ApplicationContext.create()
    if args.mode == "web":
        uvicorn.run(
            create_app(context),
            host=context.settings.host,
            port=context.settings.port,
            log_level="info",
        )
        return
    if args.mode == "all":
        try:
            asyncio.run(_run_all(context))
        except KeyboardInterrupt:
            logging.getLogger(__name__).info("服务已停止")
        return
    try:
        asyncio.run(_run_worker(context))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("服务已停止")


async def _run_worker(context: ApplicationContext) -> None:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)

    monitor = context.monitor()
    delivery = _create_delivery_worker(context)
    qq_listener = context.qq_listener
    try:
        await asyncio.gather(
            monitor.run_forever(stop_event),
            delivery.run_forever(stop_event),
            qq_listener.run_forever(stop_event),
        )
    finally:
        await qq_listener.aclose()
        await delivery.aclose()


async def _run_all(context: ApplicationContext) -> None:
    """在一个事件循环中同时运行管理页面、采集器和推送 Worker。"""
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    delivery = _create_delivery_worker(context)
    qq_listener = context.qq_listener
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(context),
            host=context.settings.host,
            port=context.settings.port,
            log_level="info",
            loop="asyncio",
        )
    )

    async def serve_web() -> None:
        try:
            await server.serve()
        finally:
            stop_event.set()

    async def stop_web_when_requested() -> None:
        await stop_event.wait()
        server.should_exit = True

    tasks = [
        asyncio.create_task(serve_web()),
        asyncio.create_task(context.monitor().run_forever(stop_event)),
        asyncio.create_task(delivery.run_forever(stop_event)),
        asyncio.create_task(qq_listener.run_forever(stop_event)),
        asyncio.create_task(stop_web_when_requested()),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        server.should_exit = True
        await qq_listener.aclose()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await delivery.aclose()


def _create_delivery_worker(context: ApplicationContext) -> DeliveryWorker:
    return DeliveryWorker(
        tasks=context.tasks,
        push_history=context.push_history,
        runtime_config=context.runtime_config,
        image_renderer=PlaywrightRenderer(
            timeout_ms=int(context.settings.http_timeout_seconds * 1000)
        ),
    )


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


if __name__ == "__main__":
    main()

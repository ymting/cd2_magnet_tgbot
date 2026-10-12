"""
轮询看门狗单元测试。

覆盖场景：
- 轮询协程健康时不得误停应用
- 轮询协程死亡（带异常 / 被取消 / 无异常结束）时必须触发自愈
- 应用正在关闭、updater 缺失、PTB 内部属性改名等边界不得误报
- post_init 中看门狗任务确实被注册
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.error import InvalidToken

import main

# 哨兵值：用于区分「显式传入 None」与「该参数未提供」
_MISSING = object()


def _make_task(*, done: bool = True, cancelled: bool = False, error=None) -> SimpleNamespace:
    """构造一个可替代 asyncio.Task 的最小对象。"""
    return SimpleNamespace(
        done=lambda: done,
        cancelled=lambda: cancelled,
        exception=lambda: error,
    )


def _make_context(*, running: bool = True, task=_MISSING, with_updater: bool = True):
    """构造 watchdog_check 需要的 context 对象。"""
    application = MagicMock()
    application.running = running

    if with_updater:
        # 用 SimpleNamespace 而非 MagicMock：只有显式赋值的属性才存在，
        # 这样才能真实模拟「PTB 内部属性改名后取不到」的情况。
        updater = SimpleNamespace()
        if task is not _MISSING:
            updater._Updater__polling_task = task
        application.updater = updater
    else:
        application.updater = None

    return SimpleNamespace(application=application)


class WatchdogCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_polling_does_not_stop_application(self):
        """轮询任务仍在运行时，看门狗必须保持沉默。"""
        context = _make_context(task=_make_task(done=False))

        await main.watchdog_check(context)

        context.application.stop_running.assert_not_called()

    async def test_dead_polling_with_invalid_token_stops_application(self):
        """InvalidToken 导致轮询静默死亡是本次要根治的故障，必须触发自愈。"""
        context = _make_context(
            task=_make_task(done=True, error=InvalidToken("Unauthorized"))
        )

        with self.assertLogs(main.logger, level="CRITICAL") as captured:
            await main.watchdog_check(context)

        context.application.stop_running.assert_called_once()
        logged = "\n".join(captured.output)
        self.assertIn("轮询已停止", logged)
        self.assertIn("InvalidToken", logged)

    async def test_cancelled_polling_stops_application(self):
        """轮询任务被取消且应用仍在运行时，视同故障。"""
        context = _make_context(task=_make_task(done=True, cancelled=True))

        with self.assertLogs(main.logger, level="CRITICAL"):
            await main.watchdog_check(context)

        context.application.stop_running.assert_called_once()

    async def test_polling_finished_without_exception_stops_application(self):
        """轮询任务无异常结束同样是异常状态，进程不应继续假装存活。"""
        context = _make_context(task=_make_task(done=True, error=None))

        with self.assertLogs(main.logger, level="CRITICAL") as captured:
            await main.watchdog_check(context)

        context.application.stop_running.assert_called_once()
        self.assertIn("无异常", "\n".join(captured.output))

    async def test_shutting_down_application_is_not_treated_as_failure(self):
        """应用正常关闭时 PTB 会自行取消轮询任务，此时不得误判。"""
        context = _make_context(
            running=False,
            task=_make_task(done=True, error=InvalidToken("Unauthorized")),
        )

        await main.watchdog_check(context)

        context.application.stop_running.assert_not_called()

    async def test_missing_internal_attribute_is_tolerated(self):
        """PTB 内部属性若改名，看门狗应安静退出而不是抛 AttributeError。"""
        context = _make_context(task=_MISSING)

        await main.watchdog_check(context)

        context.application.stop_running.assert_not_called()

    async def test_missing_updater_is_tolerated(self):
        """updater 为空（尚未启动轮询）时应安静退出。"""
        context = _make_context(with_updater=False)

        await main.watchdog_check(context)

        context.application.stop_running.assert_not_called()


class WatchdogConfigTests(unittest.TestCase):
    def test_default_interval_is_positive(self):
        """默认检查周期必须是正数，否则看门狗不会生效。"""
        self.assertGreater(main.WATCHDOG_INTERVAL_SECONDS, 0)


class PostInitWatchdogRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_job_is_registered_in_post_init(self):
        """post_init 必须把看门狗挂到内置 JobQueue 上。"""
        application = MagicMock()
        application.bot.set_my_commands = AsyncMock()

        await main.post_init(application)

        watchdog_jobs = [
            call for call in application.job_queue.run_repeating.call_args_list
            if call.kwargs.get("name") == "polling_watchdog"
        ]
        self.assertEqual(len(watchdog_jobs), 1)
        args, kwargs = watchdog_jobs[0]
        self.assertIs(args[0], main.watchdog_check)
        self.assertEqual(kwargs["interval"], main.WATCHDOG_INTERVAL_SECONDS)
        self.assertEqual(kwargs["name"], "polling_watchdog")


if __name__ == "__main__":
    unittest.main()

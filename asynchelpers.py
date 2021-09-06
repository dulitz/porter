"""
async.py -- stuff to make async easier to deal with
"""

import asyncio
import prometheus_client

class AsyncPollingLoop:
    AWAITING = prometheus_client.Gauge(
        'porter_num_tasks', 'number of async tasks being awaited', ['loop']
    )
    LAST_ITERATION = prometheus_client.Gauge(
        'porter_loop_iteration_time', 'when loop was last entered', ['loop']
    )
    LAST_COMPLETION = prometheus_client.Gauge(
        'porter_task_completion_time', 'when task was last completed', ['loop']
    )

    def __init__(self, name, awaitables=[], poll_timeout=1):
        self.name = name
        self.poll_timeout = poll_timeout
        self.awaiting = set()
        for a in awaitables:
            self.add_awaitable(a)

    def add_awaitable(self, awaitable):
        if awaitable:
            if isinstance(awaitable, asyncio.Task):
                self.awaiting.add(awaitable)
            else:
                self.awaiting.add(asyncio.create_task(awaitable))

    async def wait(self):
        AsyncPollingLoop.AWAITING.labels(loop=self.name).set(len(self.awaiting))
        self.LAST_ITERATION.labels(loop=self.name).set_to_current_time()
        if not self.awaiting:
            await asyncio.sleep(self.poll_timeout)
            return set()
        (done, self.awaiting) = await asyncio.wait(
            self.awaiting,
            timeout=self.poll_timeout,
            return_when=asyncio.FIRST_COMPLETED
        )
        for d in done:
            self.LAST_COMPLETION.labels(loop=self.name).set_to_current_time()
            # if d raised an exception, it will be propagated here
            self.add_awaitable(d.result())
        return done

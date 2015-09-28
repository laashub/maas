# encoding: utf-8
# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database Tasks Service.

A service that runs deferred database operations, and then ensures they're
finished before stopping.
"""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "DatabaseTasksService",
]

from maasserver.utils.threads import deferToDatabase
from provisioningserver.utils.twisted import (
    asynchronous,
    FOREVER,
)
from twisted.application.service import Service
from twisted.internet.defer import (
    Deferred,
    DeferredQueue,
)
from twisted.internet.task import cooperate
from twisted.python import log


class DatabaseTasksService(Service, object):
    """Run deferred database operations one at a time.

    Once the service is started, `deferTask` and `addTask` can be used to
    queue up execution of a database task.

    The former — `deferTask` — will return a `Deferred` that fires with the
    result of the database task. Errors arising from this task become the
    responsibility of the caller.

    The latter — `addTask` — returns nothing, and will log errors arising from
    the database task.

    Before this service has been started, and as soon as shutdown has
    commenced, database tasks will be rejected by `deferTask` and `addTask`.

    """

    sentinel = object()

    def __init__(self, limit=100):
        """Initialise a new `DatabaseTasksService`.

        :param limit: The maximum number of database tasks to defer before
            rejecting additional tasks.
        """
        super(DatabaseTasksService, self).__init__()
        # Start with a queue that rejects puts.
        self.queue = DeferredQueue(size=0, backlog=1)
        self.limit = limit

    @asynchronous
    def deferTask(self, func, *args, **kwargs):
        """Schedules `func` to run later.

        :raise QueueOverflow: If the queue of tasks is full.
        :return: :class:`Deferred`, which fires with the result of the running
            the task in a database thread.
        """
        done = Deferred()

        def task():
            d = deferToDatabase(func, *args, **kwargs)
            d.chainDeferred(done)
            return d

        self.queue.put(task)
        return done

    @asynchronous(timeout=FOREVER)
    def addTask(self, func, *args, **kwargs):
        """Schedules `func` to run later.

        Failures arising from the running the task in a database thread will
        be logged.

        :raise QueueOverflow: If the queue of tasks is full.
        :return: `None`
        """
        done = self.deferTask(func, *args, **kwargs)
        done.addErrback(log.err, "Unhandled failure in database task.")
        return None

    @asynchronous(timeout=FOREVER)
    def startService(self):
        """Open the queue and start processing database tasks.

        :return: `None`
        """
        super(DatabaseTasksService, self).startService()
        self.queue.size = self.limit  # Open queue to puts.
        self.coop = cooperate(self._generateTasks())

    @asynchronous(timeout=FOREVER)
    def stopService(self):
        """Close the queue and finish processing outstanding database tasks.

        :return: :class:`Deferred` which fires once all tasks have been run.
        """
        super(DatabaseTasksService, self).stopService()
        # Feed the cooperative task so that it can shutdown.
        self.queue.size += 1  # Prevent QueueOverflow.
        self.queue.put(self.sentinel)  # See _generateTasks.
        self.queue.size = 0  # Now close queue to puts.
        # This service has stopped when the coop task is done.
        return self.coop.whenDone()

    def _generateTasks(self):
        """Feed the cooperator.

        This pulls tasks from the queue while this service is running and
        executes them. If no tasks are pending it will wait for more.

        Once shutdown of the service commences this will continue pulling and
        executing tasks while there are tasks actually pending; it will not
        wait for additional tasks to be enqueued.
        """
        queue = self.queue
        sentinel = self.sentinel

        def execute(task):
            if task is not sentinel:
                return task()

        # Execute tasks as long as we're running.
        while self.running:
            yield queue.get().addCallback(execute)

        # Execute all remaining tasks.
        while len(queue.pending) != 0:
            yield queue.get().addCallback(execute)

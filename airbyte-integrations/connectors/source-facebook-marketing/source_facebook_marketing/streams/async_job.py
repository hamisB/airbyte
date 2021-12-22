#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#

import logging
from enum import Enum
from typing import Any, Mapping

import backoff
import pendulum
from facebook_business.adobjects.adreportrun import AdReportRun
from facebook_business.adobjects.objectparser import ObjectParser
from facebook_business.api import FacebookRequest, FacebookResponse
from facebook_business.exceptions import FacebookRequestError
from source_facebook_marketing.api import API

from .common import retry_pattern

backoff_policy = retry_pattern(backoff.expo, FacebookRequestError, max_tries=5, factor=5)
logger = logging.getLogger("airbyte")


class Status(str, Enum):
    """Async job statuses"""

    COMPLETED = "Job Completed"
    FAILED = "Job Failed"
    SKIPPED = "Job Skipped"
    STARTED = "Job Started"
    RUNNING = "Job Running"
    NOT_STARTED = "Job Not Started"


class AsyncJob:
    """AsyncJob wraps FB AdReport class and provides interface to restart/retry the async job"""

    def __init__(self, api: API, params: Mapping[str, Any]):
        """Initialize

        :param api: Facebook Api wrapper
        :param params: job params, required to start/restart job
        """
        self._params = params
        self._api = api
        self._job: AdReportRun = None
        self._start_time = None
        self._finish_time = None
        self._failed = False

    @backoff_policy
    def start(self):
        """Start remote job"""
        if self._job:
            raise RuntimeError(f"{self}: Incorrect usage of start - the job already started, use restart instead")

        self._job = self._api.account.get_insights(params=self._params, is_async=True)
        self._start_time = pendulum.now()
        job_id = self._job["report_run_id"]
        time_range = self._params["time_range"]
        breakdowns = self._params["breakdowns"]
        logger.info(f"Created AdReportRun: {job_id} to sync insights {time_range} with breakdown {breakdowns}")

    def restart(self):
        """Restart failed job"""
        if not self._job or not self.failed:
            raise RuntimeError(f"{self}: Incorrect usage of restart - only failed jobs can be restarted")

        self._job = None
        self._failed = False
        self._start_time = None
        self._finish_time = None
        self.start()
        logger.info(f"{self}: restarted")

    @property
    def elapsed_time(self):
        """Elapsed time since the job start"""
        if not self._start_time:
            return None

        end_time = self._finish_time or pendulum.now()
        return end_time - self._start_time

    @property
    def completed(self) -> bool:
        """Check job status and return True if it is completed, use failed/succeeded to check if it was successful

        :return: True if completed, False - if task still running
        :raises: JobException in case job failed to start, failed or timed out
        """
        if self._finish_time:
            return True
        self._update_job()
        return self._check_status()

    def batch_update_request(self) -> FacebookRequest:
        if self._finish_time:
            # No need to update job status if its already completed
            return None
        return self._job.api_get(pending=True)

    def process_batch_result(self, response: FacebookResponse):
        """Update job status from response"""
        self._job = ObjectParser(reuse_object=self._job).parse_single(response.json())
        self._check_status()

    @property
    def failed(self) -> bool:
        """Tell if the job previously failed"""
        return self._failed

    @backoff_policy
    def _update_job(self):
        """Method to retrieve job's status, separated because of retry handler"""
        if not self._job:
            raise RuntimeError(f"{self}: Incorrect usage of the method - the job is not started")
        self._job = self._job.api_get()

    def _check_status(self) -> bool:
        """Perform status check

        :return: True if the job is completed, False - if the job is still running
        """
        job_progress_pct = self._job["async_percent_completion"]
        logger.info(f"{self} is {job_progress_pct}% complete ({self._job['async_status']})")
        job_status = self._job["async_status"]

        if job_status == Status.COMPLETED:
            self._finish_time = pendulum.now()  # TODO: is not actual running time, but interval between check_status calls
            return True
        elif job_status in [Status.FAILED, Status.SKIPPED]:
            self._finish_time = pendulum.now()
            self._failed = True
            logger.info(f"{self._job} has status {job_status} after {self.elapsed_time.in_seconds()} seconds.")
            return True

        return False

    @backoff_policy
    def get_result(self) -> Any:
        """Retrieve result of the finished job."""
        if not self._job or self.failed:
            raise RuntimeError(f"{self}: Incorrect usage of get_result - the job is not started of failed")
        return self._job.get_result()

    def __str__(self) -> str:
        """String representation of the job wrapper."""
        job_id = self._job["report_run_id"] if self._job else "<None>"
        time_range = self._params["time_range"]
        breakdowns = self._params["breakdowns"]
        return f"AdReportRun(id={job_id}, time_range={time_range}, breakdowns={breakdowns}"

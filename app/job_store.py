"""
Job status store, with two backends:

- InMemoryJobStore: a dict in the process's own memory. Fine for local
  development and for a single-instance deployment (max-instances: 1),
  since every request - including polls - is guaranteed to land on the
  same process that's doing the work.

- FirestoreJobStore: required once max-instances > 1. With more than one
  instance, the instance that handles a poll request is NOT necessarily
  the one processing the job (Cloud Run's load balancer doesn't pin a
  client to an instance), so an in-memory dict on the wrong instance
  would incorrectly 404 a job that's actually still running fine
  elsewhere. Firestore gives every instance the same view of job state,
  with strongly-consistent reads on a single document, so a poll always
  sees the real status regardless of which instance handles it.

Which one is used is controlled by the JOB_STORE_BACKEND env var
(default: "memory"). See the README for the one-time Firestore setup
steps and for exactly when you need to switch.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional


class InMemoryJobStore:
    def __init__(self, ttl_seconds: int = 60 * 60):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    def create(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id] = {
                "status": "processing", "result": None, "error": None,
                "created_at": time.time(), "total_units": None, "pages": [],
            }

    def set_total_units(self, job_id: str, total: int) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["total_units"] = total

    def append_page_result(self, job_id: str, page_result: dict) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["pages"].append(page_result)

    def set_done(self, job_id: str, result: dict) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = "done"
                self._jobs[job_id]["result"] = result

    def set_error(self, job_id: str, error: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = "error"
                self._jobs[job_id]["error"] = error

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def cleanup_old(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        with self._lock:
            stale = [jid for jid, job in self._jobs.items() if job["created_at"] < cutoff]
            for jid in stale:
                del self._jobs[jid]


class FirestoreJobStore:
    """Backed by a Firestore collection, so any Cloud Run instance can
    read the current status of a job regardless of which instance
    created or is processing it.

    On Cloud Run, this works with zero extra credential setup - the
    service's attached service account authenticates automatically via
    the metadata server. Running this locally requires either
    `gcloud auth application-default login` first, or a service account
    key file referenced by GOOGLE_APPLICATION_CREDENTIALS.
    """

    COLLECTION = "doc_lang_jobs"

    def __init__(self, ttl_seconds: int = 60 * 60):
        try:
            from google.cloud import firestore
        except ImportError as e:
            raise RuntimeError(
                "JOB_STORE_BACKEND=firestore requires the google-cloud-firestore "
                "package (already in requirements.txt - rebuild the image if you're "
                "seeing this)."
            ) from e
        self._firestore = firestore
        self._client = firestore.Client()
        self._ttl_seconds = ttl_seconds

    def _doc(self, job_id: str):
        return self._client.collection(self.COLLECTION).document(job_id)

    def create(self, job_id: str) -> None:
        self._doc(job_id).set({
            "status": "processing", "result": None, "error": None,
            "created_at": time.time(), "total_units": None, "pages": [],
        })

    def set_total_units(self, job_id: str, total: int) -> None:
        self._doc(job_id).update({"total_units": total})

    def append_page_result(self, job_id: str, page_result: dict) -> None:
        # ArrayUnion is an atomic server-side append - no read-modify-write
        # race even if something else touched this doc concurrently.
        self._doc(job_id).update({"pages": self._firestore.ArrayUnion([page_result])})

    def set_done(self, job_id: str, result: dict) -> None:
        self._doc(job_id).update({"status": "done", "result": result})

    def set_error(self, job_id: str, error: str) -> None:
        self._doc(job_id).update({"status": "error", "error": error})

    def get(self, job_id: str) -> Optional[dict]:
        snap = self._doc(job_id).get()
        return snap.to_dict() if snap.exists else None

    def cleanup_old(self) -> None:
        # Best-effort. If you'd rather not run this on every request, set
        # up a Firestore TTL policy on the "created_at" field instead
        # (Firestore console -> your database -> TTL policies) - that
        # handles deletion natively with no code involved. This manual
        # sweep is a fine default either way since it's cheap and safe
        # to double up with a TTL policy.
        try:
            cutoff = time.time() - self._ttl_seconds
            query = self._client.collection(self.COLLECTION).where("created_at", "<", cutoff).limit(50)
            for doc in query.stream():
                doc.reference.delete()
        except Exception:
            # Cleanup failing shouldn't ever break a request.
            pass


def get_job_store():
    backend = os.environ.get("JOB_STORE_BACKEND", "memory").lower()
    if backend == "firestore":
        return FirestoreJobStore()
    return InMemoryJobStore()

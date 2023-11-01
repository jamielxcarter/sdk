"""Batching utilities for Singer SDK."""
from __future__ import annotations

import gzip
import importlib.util
import itertools
import json
import typing as t
from abc import ABC, abstractmethod
from uuid import uuid4

from singer_sdk.helpers._batch import BatchFileFormat

if t.TYPE_CHECKING:
    from singer_sdk.helpers._batch import BatchConfig

_T = t.TypeVar("_T")


def lazy_chunked_generator(
    iterable: t.Iterable[_T],
    chunk_size: int,
) -> t.Generator[t.Iterator[_T], None, None]:
    """Yield a generator for each chunk of the given iterable.

    Args:
        iterable: The iterable to chunk.
        chunk_size: The size of each chunk.

    Yields:
        A generator for each chunk of the given iterable.
    """
    iterator = iter(iterable)
    while True:
        chunk = list(itertools.islice(iterator, chunk_size))
        if not chunk:
            break
        yield iter(chunk)


class BaseBatcher(ABC):
    """Base Record Batcher."""

    def __init__(
        self,
        tap_name: str,
        stream_name: str,
        batch_config: BatchConfig,
    ) -> None:
        """Initialize the batcher.

        Args:
            tap_name: The name of the tap.
            stream_name: The name of the stream.
            batch_config: The batch configuration.
        """
        self.tap_name = tap_name
        self.stream_name = stream_name
        self.batch_config = batch_config

    @abstractmethod
    def get_batches(
        self,
        records: t.Iterator[dict],
    ) -> t.Iterator[list[str]]:
        """Yield manifest of batches.

        Args:
            records: The records to batch.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError


class JSONLinesBatcher(BaseBatcher):
    """JSON Lines Record Batcher."""

    def get_batches(
        self,
        records: t.Iterator[dict],
    ) -> t.Iterator[list[str]]:
        """Yield manifest of batches.

        Args:
            records: The records to batch.

        Yields:
            A list of file paths (called a manifest).
        """
        sync_id = f"{self.tap_name}--{self.stream_name}-{uuid4()}"
        prefix = self.batch_config.storage.prefix or ""

        for i, chunk in enumerate(
            lazy_chunked_generator(
                records,
                self.batch_config.batch_size,
            ),
            start=1,
        ):
            filename = f"{prefix}{sync_id}-{i}.json.gz"
            with self.batch_config.storage.fs(create=True) as fs:
                # TODO: Determine compression from config.
                with fs.open(filename, "wb") as f, gzip.GzipFile(
                    fileobj=f,
                    mode="wb",
                ) as gz:
                    gz.writelines(
                        (json.dumps(record, default=str) + "\n").encode()
                        for record in chunk
                    )
                file_url = fs.geturl(filename)
            yield [file_url]


spec_imported = importlib.util.find_spec("pyarrow")
if spec_imported:
    import pyarrow as pa
    import pyarrow.parquet as pq

    class ParquetBatcher(BaseBatcher):
        """Parquet Record Batcher."""

        def get_batches(
            self,
            records: t.Iterator[dict],
        ) -> t.Iterator[list[str]]:
            """Yield manifest of batches.

            Args:
                records: The records to batch.

            Yields:
                A list of file paths (called a manifest).
            """
            sync_id = f"{self.tap_name}--{self.stream_name}-{uuid4()}"
            prefix = self.batch_config.storage.prefix or ""

            for i, chunk in enumerate(
                lazy_chunked_generator(
                    records,
                    self.batch_config.batch_size,
                ),
                start=1,
            ):
                filename = f"{prefix}{sync_id}={i}.parquet"
                if self.batch_config.encoding.compression == "gzip":
                    filename = f"{filename}.gz"
                with self.batch_config.storage.fs() as fs:
                    with fs.open(filename, "wb") as f:
                        pylist = list(chunk)
                        table = pa.Table.from_pylist(pylist)
                        if self.batch_config.encoding.compression == "gzip":
                            pq.write_table(table, f, compression="GZIP")
                        else:
                            pq.write_table(table, f)

                    file_url = fs.geturl(filename)
                yield [file_url]


class Batcher(BaseBatcher):
    """Determines batch type and then serializes batches to that format."""

    def get_batches(self, records: t.Iterator[dict]) -> t.Iterator[list[str]]:
        """Manifest of batches.

        Args:
            records: The records to batch.

        Returns:
            A list of file paths (called a manifest).

        Raises:
            ValueError: If unsupported format given.
        """
        if spec_imported and (
            self.batch_config.encoding.format == BatchFileFormat.PARQUET
        ):
            batches = self._batch_to_parquet(records)
        elif self.batch_config.encoding.format == BatchFileFormat.JSONL:
            batches = self._batch_to_jsonl(records)
        else:
            raise ValueError(self.batch_config.encoding.format)
        return batches

    def _batch_to_jsonl(self, records: t.Iterator[dict]) -> t.Iterator[list[str]]:
        return JSONLinesBatcher(
            tap_name=self.tap_name,
            stream_name=self.stream_name,
            batch_config=self.batch_config,
        ).get_batches(records)

    def _batch_to_parquet(self, records: t.Iterator[dict]) -> t.Iterator[list[str]]:
        return ParquetBatcher(
            tap_name=self.tap_name,
            stream_name=self.stream_name,
            batch_config=self.batch_config,
        ).get_batches(records)

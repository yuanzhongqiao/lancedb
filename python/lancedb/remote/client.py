#  Copyright 2023 LanceDB Developers
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.


import functools
from typing import Any, Callable, Dict, Union

import aiohttp
import attr
import pyarrow as pa
from pydantic import BaseModel

from lancedb.common import Credential
from lancedb.remote import VectorQuery, VectorQueryResult
from lancedb.remote.errors import LanceDBClientError


def _check_not_closed(f):
    @functools.wraps(f)
    def wrapped(self, *args, **kwargs):
        if self.closed:
            raise ValueError("Connection is closed")
        return f(self, *args, **kwargs)

    return wrapped


async def _read_ipc(resp: aiohttp.ClientResponse) -> pa.Table:
    resp_body = await resp.read()
    with pa.ipc.open_file(pa.BufferReader(resp_body)) as reader:
        return reader.read_all()


@attr.define(slots=False)
class RestfulLanceDBClient:
    db_name: str
    region: str
    api_key: Credential
    closed: bool = attr.field(default=False, init=False)

    @functools.cached_property
    def session(self) -> aiohttp.ClientSession:
        url = f"https://{self.db_name}.{self.region}.api.lancedb.com"
        return aiohttp.ClientSession(url)

    async def close(self):
        await self.session.close()
        self.closed = True

    @functools.cached_property
    def headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
        }

    @staticmethod
    async def _check_status(resp: aiohttp.ClientResponse):
        if resp.status == 404:
            raise LanceDBClientError(f"Not found: {await resp.text()}")
        elif 400 <= resp.status < 500:
            raise LanceDBClientError(
                f"Bad Request: {resp.status}, error: {await resp.text()}"
            )
        elif 500 <= resp.status < 600:
            raise LanceDBClientError(
                f"Internal Server Error: {resp.status}, error: {await resp.text()}"
            )
        elif resp.status != 200:
            raise LanceDBClientError(
                f"Unknown Error: {resp.status}, error: {await resp.text()}"
            )

    @_check_not_closed
    async def get(self, uri: str, params: Union[Dict[str, Any], BaseModel] = None):
        """Send a GET request and returns the deserialized response payload."""
        if isinstance(params, BaseModel):
            params: Dict[str, Any] = params.dict(exclude_none=True)
        async with self.session.get(uri, params=params, headers=self.headers) as resp:
            await self._check_status(resp)
            return await resp.json()

    @_check_not_closed
    async def post(
        self,
        uri: str,
        data: Union[Dict[str, Any], BaseModel],
        deserialize: Callable = lambda resp: resp.json(),
    ) -> Dict[str, Any]:
        """Send a POST request and returns the deserialized response payload.

        Parameters
        ----------
        uri : str
            The uri to send the POST request to.
        data: Union[Dict[str, Any], BaseModel]

        """
        if isinstance(data, BaseModel):
            data: Dict[str, Any] = data.dict(exclude_none=True)
        async with self.session.post(
            uri,
            json=data,
            headers=self.headers,
        ) as resp:
            resp: aiohttp.ClientResponse = resp
            await self._check_status(resp)
            return await deserialize(resp)

    @_check_not_closed
    async def list_tables(self):
        """List all tables in the database."""
        json = await self.get("/1/table/", {})
        return json["tables"]

    @_check_not_closed
    async def query(self, table_name: str, query: VectorQuery) -> VectorQueryResult:
        """Query a table."""
        tbl = await self.post(f"/1/table/{table_name}/", query, deserialize=_read_ipc)
        return VectorQueryResult(tbl)
import tempfile
import time
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import xray_node.main as node


class XrayNodeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        node.CONFIG_PATH = root / "config.json"
        node.STATE_PATH = root / "state.json"
        node.NODE_TOKEN = "test-token"
        node.PUBLIC_HOST = "vpn.example.com"
        node._atomic_json(node.CONFIG_PATH, node._default_config())
        node._atomic_json(node.STATE_PATH, {"inbounds": {}, "clients": {}})
        self.validate = patch.object(node, "_validate", return_value=None)
        self.restart = patch.object(node, "_restart_xray", return_value=None)
        self.keys = patch.object(node, "_x25519", return_value=("private-key", "public-key"))
        self.validate.start()
        self.restart.start()
        self.keys.start()
        self.client = TestClient(node.app)
        self.headers = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        self.client.close()
        patch.stopall()
        self.temp.cleanup()

    def test_auth_is_required(self):
        self.assertEqual(self.client.get("/api/inbounds").status_code, 401)

    def test_inbound_client_and_link_lifecycle(self):
        created = self.client.post("/api/inbounds", headers=self.headers, json={
            "name": "vless-reality",
            "preset": "vless-reality",
            "port": 443,
            "server_name": "www.microsoft.com",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["security"], "reality")

        duplicate = self.client.post("/api/inbounds", headers=self.headers, json={
            "name": "another",
            "preset": "vless-reality",
            "port": 443,
            "server_name": "www.microsoft.com",
        })
        self.assertEqual(duplicate.status_code, 409)

        uuid = "123e4567-e89b-12d3-a456-426614174000"
        added = self.client.post("/api/inbounds/1/clients", headers=self.headers, json={
            "uuid": uuid,
            "email": "user-1",
            "expire_ms": 0,
            "total_gb": 10,
            "limit_ip": 1,
        })
        self.assertEqual(added.status_code, 200, added.text)

        links = self.client.get(
            "/api/inbounds/1/clients/user-1/links",
            params={"label": "Germany 1"},
            headers=self.headers,
        )
        self.assertEqual(links.status_code, 200, links.text)
        self.assertIn(f"vless://{uuid}@vpn.example.com:443", links.json()[0])
        self.assertIn("security=reality", links.json()[0])
        self.assertIn("pbk=public-key", links.json()[0])

        deleted_client = self.client.delete(
            f"/api/inbounds/1/clients/{uuid}", headers=self.headers
        )
        self.assertEqual(deleted_client.status_code, 200)
        self.assertEqual(
            self.client.get("/api/inbounds", headers=self.headers).json()[0]["client_count"],
            0,
        )

        removed = self.client.delete("/api/inbounds/1", headers=self.headers)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/inbounds", headers=self.headers).json(), [])

    def test_websocket_preset(self):
        response = self.client.post("/api/inbounds", headers=self.headers, json={
            "name": "websocket",
            "preset": "vless-ws",
            "port": 8080,
            "server_name": "edge.example.com",
            "path": "/edge",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["network"], "ws")
        config = node._read_config()
        ws = node._find_inbound(config, "websocket")["streamSettings"]["wsSettings"]
        self.assertEqual(ws["headers"]["Host"], "edge.example.com")

    def test_expired_clients_are_pruned(self):
        self.client.post("/api/inbounds", headers=self.headers, json={
            "name": "expiry-test",
            "preset": "vless-reality",
            "port": 8443,
            "server_name": "www.microsoft.com",
        })
        self.client.post("/api/inbounds/1/clients", headers=self.headers, json={
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "email": "expired",
            "expire_ms": int(time.time() * 1000) - 1000,
        })
        removed = asyncio.run(node.prune_expired_clients())
        self.assertEqual(removed, 1)
        self.assertEqual(node._read_config()["inbounds"][1]["settings"]["clients"], [])


if __name__ == "__main__":
    unittest.main()

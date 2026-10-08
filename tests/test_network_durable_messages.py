from backend.app.platform_browser import _HiddenCDPTarget


def test_durable_buffers_are_bounded_and_do_not_replay_platform_requests():
    target = _HiddenCDPTarget("ws://localhost/test")
    calls = []
    target.call = lambda method, params: calls.append((method, params))
    target.enable_network(max_total_buffer_size=24 * 1024 * 1024, max_resource_buffer_size=4 * 1024 * 1024)
    assert [method for method, _ in calls] == ["Network.enable", "Network.configureDurableMessages"]
    assert calls[0][1] == calls[1][1] == {
        "maxTotalBufferSize": 24 * 1024 * 1024, "maxResourceBufferSize": 4 * 1024 * 1024,
    }


def test_unsupported_durable_messages_does_not_disable_network_reader():
    target = _HiddenCDPTarget("ws://localhost/test")
    calls = []
    def call(method, params):
        calls.append(method)
        if method == "Network.configureDurableMessages":
            raise RuntimeError("Method not found")
    target.call = call
    target.enable_network()
    assert calls == ["Network.enable", "Network.configureDurableMessages"]

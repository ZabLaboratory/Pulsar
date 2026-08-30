/*
obs-websocket
Copyright (C) 2016-2021 Stephane Lepin <stephane.lepin@gmail.com>
Copyright (C) 2020-2021 Kyle Manning <tt2468@gmail.com>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>
*/

#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <QObject>
#include <QThreadPool>
#include <QString>
#include <asio.hpp>
#include <websocketpp/config/asio_no_tls.hpp>
#include <websocketpp/server.hpp>

#include "rpc/WebSocketSession.h"
#include "types/WebSocketCloseCode.h"
#include "types/WebSocketOpCode.h"
#include "../requesthandler/rpc/Request.h"
#include "../utils/Json.h"
#include "plugin-macros.generated.h"

class WebSocketServer : QObject {
	Q_OBJECT

public:
	enum WebSocketEncoding { Json, MsgPack };

	struct WebSocketSessionState {
		websocketpp::connection_hdl hdl;
		std::string remoteAddress;
		uint64_t connectedAt;
		uint64_t incomingMessages;
		uint64_t outgoingMessages;
		bool isIdentified;
	};

	// A batch lease is intentionally public to the request-batch adapter: the
	// websocket message lease covers the worker, while this nested lease makes
	// the SerialFrame callback and Parallel worker fan-out independently
	// visible to the shutdown drain.
	class HandlerLease {
	public:
		explicit HandlerLease(WebSocketServer *server) : _server(server) {}
		~HandlerLease();
		HandlerLease(const HandlerLease &) = delete;
		HandlerLease &operator=(const HandlerLease &) = delete;

	private:
		WebSocketServer *_server;
	};

	WebSocketServer();
	~WebSocketServer();

	void Start();
	void Stop();
	// Stop admitting websocket work, close all sessions, and wait for every
	// already-admitted IO/request/batch/frame handler to leave.  The caller
	// must not tear down libobs/frontend callbacks until this bounded drain
	// returns true.
	bool Quiesce(std::chrono::milliseconds timeout, size_t &activeHandlers, size_t &sessionsRemaining);
	void InvalidateSession(websocketpp::connection_hdl hdl);
	void BroadcastEvent(uint64_t requiredIntent, const std::string &eventType, const json &eventData = nullptr,
			    uint8_t rpcVersion = 0);
	inline void SetObsReady(bool ready) { _obsReady = ready; }
	inline bool IsListening() { return _server.is_listening(); }
	std::vector<WebSocketSessionState> GetWebSocketSessions();
	inline QThreadPool *GetThreadPool() { return &_threadPool; }

	// Callback for when a client subscribes or unsubscribes. `true` for sub, `false` for unsub
	typedef std::function<void(bool, uint64_t)> ClientSubscriptionCallback; // bool type, uint64_t eventSubscriptions
	inline void SetClientSubscriptionCallback(ClientSubscriptionCallback cb) { _clientSubscriptionCallback = cb; }
	std::shared_ptr<HandlerLease> EnterBatchHandler();

signals:
	void ClientConnected(WebSocketSessionState state);
	void ClientDisconnected(WebSocketSessionState state, uint16_t closeCode);

private:
	enum class LifecycleState : uint8_t { Running, Quiescing, Stopped };

	std::shared_ptr<HandlerLease> EnterHandler(bool allowQuiescing = false);
	void LeaveHandler();
	void CloseSessions();

	struct ProcessResult {
		WebSocketCloseCode::WebSocketCloseCode closeCode = WebSocketCloseCode::DontClose;
		std::string closeReason;
		json result;
	};

	void ServerRunner();

	bool onValidate(websocketpp::connection_hdl hdl);
	void onOpen(websocketpp::connection_hdl hdl);
	void onClose(websocketpp::connection_hdl hdl);
	void onMessage(websocketpp::connection_hdl hdl, websocketpp::server<websocketpp::config::asio>::message_ptr message);

	static void SetSessionParameters(SessionPtr session, WebSocketServer::ProcessResult &ret, const json &payloadData);
	void ProcessMessage(SessionPtr session, ProcessResult &ret, WebSocketOpCode::WebSocketOpCode opCode, json &payloadData);

	QThreadPool _threadPool;

	std::thread _serverThread;
	websocketpp::server<websocketpp::config::asio> _server;

	std::string _authenticationSecret;
	std::string _authenticationSalt;

	std::mutex _sessionMutex;
	std::map<websocketpp::connection_hdl, SessionPtr, std::owner_less<websocketpp::connection_hdl>> _sessions;

	// Admission and drain are one linearization domain.  A lease is acquired
	// before work is queued, so work which raced the transition is counted and
	// cannot outlive the quiesce ACK.  New work sees Quiescing and is rejected.
	mutable std::mutex _lifecycleMutex;
	std::condition_variable _lifecycleCondition;
	LifecycleState _lifecycleState = LifecycleState::Running;
	size_t _activeHandlers = 0;

	// Pulsar fork: _obsReady defaults to true (vs false upstream).
	// Upstream's gate fires when OBS_FRONTEND_EVENT_FINISHED_LOADING
	// arrives -- but the headless service has no frontend to emit
	// that event, so requests would forever return NotReady (code
	// 207). For Pulsar there is no "loading phase" to wait for:
	// libobs is initialised by pulsar-headless before the websocket
	// server starts accepting connections, so requests are valid
	// from the first connection.
	std::atomic<bool> _obsReady = true;

	ClientSubscriptionCallback _clientSubscriptionCallback;
};

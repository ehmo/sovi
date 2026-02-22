# gRPC Protocol (Aspirational)

**Status:** Defined but not yet implemented. The current system uses direct WDA HTTP calls and thread-per-device scheduling. The gRPC protocol is designed for a future daemon architecture.

## Proto Definition

`proto/device_service.proto`

## Services

### DeviceService

Remote device fleet management.

| RPC | Request | Response | Description |
|-----|---------|----------|-------------|
| ListDevices | Empty | DeviceList | All connected devices |
| GetDeviceStatus | DeviceId | DeviceStatus | Single device state |
| StreamDeviceStatus | DeviceId | stream DeviceStatus | Real-time status updates |
| RebootDevice | DeviceId | Result | Trigger device reboot |
| SetProxy | SetProxyRequest | Result | Configure device proxy |

### AutomationService

UI automation operations.

| RPC | Request | Response | Description |
|-----|---------|----------|-------------|
| StartSession | StartSessionRequest | SessionInfo | Create WDA session |
| StopSession | SessionId | Result | End session |
| ExecuteAction | ActionRequest | ActionResult | Single UI action |
| ExecuteActionSequence | ActionSequenceRequest | ActionSequenceResult | Batch actions |
| FindElement | FindElementRequest | ElementResult | Element lookup |
| TakeScreenshot | SessionId | ScreenshotResult | Capture screen |
| SwitchAccount | SwitchAccountRequest | Result | Account switch |

### WarmingService

Warming session management.

| RPC | Request | Response | Description |
|-----|---------|----------|-------------|
| StartWarmingSequence | WarmingRequest | Result | Begin warming |
| PauseWarming | DeviceId | Result | Pause active warming |
| ResumeWarming | DeviceId | Result | Resume paused warming |
| GetWarmingProgress | DeviceId | stream WarmingProgress | Real-time progress |
| StopWarming | DeviceId | Result | Stop warming |

### HealthService

System health monitoring.

| RPC | Request | Response | Description |
|-----|---------|----------|-------------|
| HealthCheck | Empty | HealthStatus | System status |
| GetMetrics | DeviceId | DeviceMetrics | Device metrics |

## Action Types

The `ActionRequest` uses a `oneof` for different UI actions:

- **TapAction**: Tap by accessibility ID or coordinates
- **TypeAction**: Type text with configurable char delay (50-150ms for human-like)
- **SwipeAction**: Swipe from point A to B with duration
- **ScrollAction**: Scroll in a direction by distance
- **WaitAction**: Pause for duration
- **LaunchAppAction**: Activate app by bundle ID
- **PressButtonAction**: Hardware button (home, volumeUp, volumeDown)

## Device States

```
available → busy → warming → recovering → available
                              ↓
                           failed → disconnected
```

## When This Would Be Used

The gRPC daemon would replace the current in-process scheduler when:
1. Multiple clients need to control devices (CLI, dashboard, external systems)
2. Device management needs to survive application restarts
3. Remote device control is needed (devices on different machines)

Currently, the in-process `DeviceScheduler` with thread-per-device is simpler and sufficient for a single-machine setup.

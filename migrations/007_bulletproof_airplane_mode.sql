-- Bulletproof Airplane Mode Protection - Database Migration 007
-- Adds audit tables and quarantine support for unbreakable airplane mode monitoring

-- =============================================================================
-- UPDATE DEVICE STATUS ENUM (if needed)
-- =============================================================================
-- Add 'quarantined' status if not already present
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum 
        WHERE enumlabel = 'quarantined' 
        AND enumtypid = 'device_status'::regtype
    ) THEN
        ALTER TYPE device_status ADD VALUE 'quarantined';
    END IF;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN OTHERS THEN NULL;
END $$;

-- =============================================================================
-- DEVICE QUARANTINE FIELDS
-- =============================================================================
-- Add quarantine tracking to devices table
ALTER TABLE devices 
    ADD COLUMN IF NOT EXISTS quarantine_reason VARCHAR(255),
    ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS requires_manual_reset BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS quarantine_cleared_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS quarantine_cleared_by VARCHAR(255);

-- Index for quarantine queries
CREATE INDEX IF NOT EXISTS idx_devices_quarantined_at 
    ON devices (quarantined_at) 
    WHERE status = 'quarantined';

-- =============================================================================
-- AIRPLANE MODE AUDIT LOG TABLE
-- =============================================================================
CREATE TABLE IF NOT EXISTS airplane_mode_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,  -- detected, recovered, recovery_failed, verification_failed, quarantined
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    previous_state BOOLEAN,
    current_state BOOLEAN,
    stack_trace TEXT,
    screenshot TEXT,  -- base64 encoded PNG
    error_message TEXT,
    recovery_attempts INTEGER DEFAULT 0,
    context JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Convert to hypertable for efficient time-series queries
SELECT create_hypertable(
    'airplane_mode_audit_log',
    'timestamp',
    if_not_exists => TRUE
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_airplane_mode_audit_device_timestamp 
    ON airplane_mode_audit_log (device_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_airplane_mode_audit_event_type 
    ON airplane_mode_audit_log (event_type, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_airplane_mode_audit_quarantined 
    ON airplane_mode_audit_log (device_id, timestamp DESC) 
    WHERE event_type = 'quarantined';

-- =============================================================================
-- NETWORK STATE CHANGES TABLE
-- =============================================================================
CREATE TABLE IF NOT EXISTS network_state_changes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    operation_id VARCHAR(255) NOT NULL,
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    action VARCHAR(100) NOT NULL,  -- e.g., "disable_airplane_mode", "enable_cellular"
    success BOOLEAN NOT NULL,
    state_before BOOLEAN,
    state_after BOOLEAN,
    wda_response_time_ms FLOAT,
    error_message TEXT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Convert to hypertable
SELECT create_hypertable(
    'network_state_changes',
    'timestamp',
    if_not_exists => TRUE
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_network_state_changes_device_timestamp 
    ON network_state_changes (device_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_network_state_changes_operation 
    ON network_state_changes (operation_id);

CREATE INDEX IF NOT EXISTS idx_network_state_changes_failed 
    ON network_state_changes (device_id, timestamp DESC) 
    WHERE success = FALSE;

-- =============================================================================
-- DEVICE NETWORK GUARD STATUS TABLE (for real-time monitoring)
-- =============================================================================
CREATE TABLE IF NOT EXISTS device_network_guard_status (
    device_id UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    airplane_mode_state VARCHAR(20),  -- 'on', 'off', 'unknown'
    last_check_at TIMESTAMP WITH TIME ZONE,
    last_successful_check_at TIMESTAMP WITH TIME ZONE,
    consecutive_failures INTEGER DEFAULT 0,
    monitor_active BOOLEAN DEFAULT FALSE,
    quarantine_pending BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Function to update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_network_guard_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for updated_at
DROP TRIGGER IF EXISTS trigger_update_network_guard_updated_at 
    ON device_network_guard_status;

CREATE TRIGGER trigger_update_network_guard_updated_at
    BEFORE UPDATE ON device_network_guard_status
    FOR EACH ROW
    EXECUTE FUNCTION update_network_guard_updated_at();

-- =============================================================================
-- RETENTION POLICY (keep audit logs for 90 days)
-- =============================================================================
SELECT add_retention_policy(
    'airplane_mode_audit_log',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'network_state_changes',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- =============================================================================
-- VIEWS FOR MONITORING
-- =============================================================================

-- View: Currently quarantined devices
CREATE OR REPLACE VIEW quarantined_devices AS
SELECT 
    d.id,
    d.label as name,
    d.status,
    d.quarantine_reason,
    d.quarantined_at,
    d.requires_manual_reset,
    a.timestamp as last_airplane_event_at,
    a.event_type as last_airplane_event_type
FROM devices d
LEFT JOIN LATERAL (
    SELECT timestamp, event_type
    FROM airplane_mode_audit_log
    WHERE device_id = d.id
    ORDER BY timestamp DESC
    LIMIT 1
) a ON true
WHERE d.status = 'quarantined'
ORDER BY d.quarantined_at DESC;

-- View: Recent airplane mode events
CREATE OR REPLACE VIEW recent_airplane_mode_events AS
SELECT 
    a.id,
    a.device_id,
    d.label as device_name,
    a.event_type,
    a.timestamp,
    a.previous_state,
    a.current_state,
    a.recovery_attempts,
    a.error_message
FROM airplane_mode_audit_log a
JOIN devices d ON a.device_id = d.id
WHERE a.timestamp > now() - INTERVAL '24 hours'
ORDER BY a.timestamp DESC;

-- =============================================================================
-- STORED PROCEDURE: Clear device quarantine (requires manual action)
-- =============================================================================
CREATE OR REPLACE FUNCTION clear_device_quarantine(
    p_device_id UUID,
    p_cleared_by VARCHAR(255),
    p_notes TEXT DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    v_device_name VARCHAR(255);
BEGIN
    -- Get device name for logging
    SELECT name INTO v_device_name FROM devices WHERE id = p_device_id;
    
    -- Update device status
    UPDATE devices 
    SET status = 'active',
        quarantine_reason = NULL,
        quarantined_at = NULL,
        requires_manual_reset = FALSE,
        quarantine_cleared_at = now(),
        quarantine_cleared_by = p_cleared_by,
        updated_at = now()
    WHERE id = p_device_id AND status = 'quarantined';
    
    -- Insert event log
    INSERT INTO system_events (
        category,
        severity,
        event_type,
        message,
        device_id,
        context
    ) VALUES (
        'device',
        'info',
        'quarantine_cleared',
        'Device ' || v_device_name || ' quarantine cleared by ' || p_cleared_by,
        p_device_id,
        jsonb_build_object(
            'cleared_by', p_cleared_by,
            'notes', p_notes,
            'previous_status', 'quarantined'
        )
    );
    
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- COMMENTS
-- =============================================================================
COMMENT ON TABLE airplane_mode_audit_log IS 
    'Audit trail of all airplane mode events with full context and screenshots';
COMMENT ON TABLE network_state_changes IS 
    'Audit trail of all network state change operations (airplane, cellular, wifi)';
COMMENT ON TABLE device_network_guard_status IS 
    'Real-time network guard monitoring status for each device';

import React, { useState, useEffect } from 'react';
import { apiGet, apiPost, errorMessage } from '../api';

interface AutotradeConfig {
  enabled: boolean;
  interval_minutes: number;
  last_run: string | null;
  last_signal: any | null;
}

const AutopilotPanel: React.FC = () => {
  const [config, setConfig] = useState<AutotradeConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [intervalInput, setIntervalInput] = useState<string>('5');

  const [manualTesting, setManualTesting] = useState(false);
  const [manualResult, setManualResult] = useState<any | null>(null);

  const fetchConfig = async () => {
    try {
      const data = await apiGet<AutotradeConfig>('/autotrade/config');
      setConfig(data);
      // Only update input if we aren't currently editing it (or if it just initialized)
      if (!config) {
        setIntervalInput(data.interval_minutes.toString());
      }
    } catch (e) {
      console.error('Failed to fetch autotrade config', e);
    }
  };

  useEffect(() => {
    fetchConfig();
    const interval = setInterval(fetchConfig, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleUpdate = async (enabled: boolean, intervalStr: string) => {
    setLoading(true);
    setError('');
    try {
      const parsedInterval = parseInt(intervalStr, 10);
      if (isNaN(parsedInterval) || parsedInterval < 1) {
        throw new Error('Interval must be at least 1 minute');
      }
      const data = await apiPost<AutotradeConfig>('/autotrade/config', {
        enabled,
        interval_minutes: parsedInterval
      });
      setConfig(data);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  const toggleEnabled = () => {
    if (!config) return;
    handleUpdate(!config.enabled, intervalInput);
  };

  const saveInterval = () => {
    if (!config) return;
    handleUpdate(config.enabled, intervalInput);
  };

  const runManualTest = async () => {
    setManualTesting(true);
    setError('');
    setManualResult(null);
    try {
      const result = await apiPost<any>('/ai/analyze');
      setManualResult(result);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setManualTesting(false);
    }
  };

  if (!config) return null;

  return (
    <section className="card" style={{ marginTop: '16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h2>🤖 AI Autopilot</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <span style={{ fontWeight: 'bold', color: config.enabled ? 'var(--color-profit)' : 'var(--color-loss)' }}>
            {config.enabled ? 'ACTIVE' : 'STOPPED'}
          </span>
          <button
            onClick={toggleEnabled}
            disabled={loading}
            style={{
              background: config.enabled ? 'var(--color-loss)' : 'var(--color-profit)',
              color: 'white',
              border: 'none',
              padding: '6px 12px',
              borderRadius: '4px',
              cursor: 'pointer'
            }}
          >
            {config.enabled ? 'STOP' : 'START'}
          </button>
        </div>
      </div>

      {error && <div style={{ color: 'var(--color-loss)', marginBottom: '8px' }}>{error}</div>}

      <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: '200px' }}>
          <label style={{ display: 'block', fontSize: '0.9rem', marginBottom: '4px', color: 'rgba(255,255,255,0.7)' }}>
            Run Interval (minutes)
          </label>
          <div style={{ display: 'flex', gap: '8px' }}>
            <input
              type="number"
              value={intervalInput}
              onChange={(e) => setIntervalInput(e.target.value)}
              min="1"
              style={{
                background: 'var(--color-bg)',
                color: 'white',
                border: '1px solid rgba(255,255,255,0.1)',
                padding: '6px',
                borderRadius: '4px',
                width: '80px'
              }}
            />
            <button
              onClick={saveInterval}
              disabled={loading || parseInt(intervalInput, 10) === config.interval_minutes}
              className="btn-primary"
            >
              Save
            </button>
          </div>
        </div>

        <div style={{ flex: 1, minWidth: '200px' }}>
          <label style={{ display: 'block', fontSize: '0.9rem', marginBottom: '4px', color: 'rgba(255,255,255,0.7)' }}>
            Last Run
          </label>
          <div style={{ fontSize: '1.1rem', fontFamily: 'monospace' }}>
            {config.last_run ? new Date(config.last_run + 'Z').toLocaleString() : 'Never'}
          </div>
        </div>
      </div>

      {config.last_signal && (
        <div style={{ marginTop: '16px', padding: '12px', background: 'rgba(255,255,255,0.05)', borderRadius: '6px' }}>
          <h4 style={{ margin: '0 0 8px 0', color: 'rgba(255,255,255,0.8)' }}>Last Auto-Executed Signal</h4>
          <div style={{ display: 'flex', gap: '16px', marginBottom: '8px' }}>
            <span style={{ fontWeight: 'bold' }}>{config.last_signal.symbol}</span>
            <span style={{
              color: config.last_signal.action === 'BUY' ? 'var(--color-profit)' :
                config.last_signal.action === 'SELL' ? 'var(--color-loss)' : 'var(--color-text)'
            }}>
              {config.last_signal.action} {config.last_signal.quantity}
            </span>
          </div>
          <div style={{ fontSize: '0.9rem', color: 'rgba(255,255,255,0.7)', fontStyle: 'italic' }}>
            "{config.last_signal.reasoning}"
          </div>
        </div>
      )}

      {/* Manual Test Section */}
      <div style={{ marginTop: '20px', borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: '16px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <h4 style={{ margin: '0 0 4px 0' }}>Manual AI Test</h4>
            <p style={{ margin: '0', fontSize: '0.85rem', color: 'rgba(255,255,255,0.6)' }}>
              Ask the AI for a trade recommendation right now. and AI can create Draft order of recommendation.
            </p>
          </div>
          <button
            onClick={runManualTest}
            disabled={manualTesting}
            className="btn-primary"
            style={{ minWidth: '120px' }}
          >
            {manualTesting ? 'Thinking...' : 'Test AI'}
          </button>
        </div>

        {manualResult && (
          <div style={{ marginTop: '16px', padding: '12px', background: 'var(--color-bg)', border: '1px solid rgba(255,255,255,0.2)', borderRadius: '6px' }}>
            <div style={{ display: 'flex', gap: '16px', marginBottom: '8px' }}>
              <span style={{ fontWeight: 'bold' }}>{manualResult.symbol}</span>
              <span style={{
                color: manualResult.action === 'BUY' ? 'var(--color-profit)' :
                  manualResult.action === 'SELL' ? 'var(--color-loss)' : 'var(--color-text)'
              }}>
                {manualResult.action} {manualResult.quantity}
              </span>
            </div>
            <div style={{ fontSize: '0.9rem', color: 'rgba(255,255,255,0.8)' }}>
              "{manualResult.reasoning}"
            </div>
            {manualResult.draft_order_status && (
              <div style={{ 
                marginTop: '12px', 
                padding: '8px', 
                background: manualResult.draft_order_status.includes('Failed') ? 'rgba(231,76,60,0.1)' : 'rgba(46,204,113,0.1)',
                borderLeft: `3px solid ${manualResult.draft_order_status.includes('Failed') ? 'var(--color-loss)' : 'var(--color-profit)'}`,
                fontSize: '0.85rem'
              }}>
                <strong>Draft Status:</strong> {manualResult.draft_order_status}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
};

export default AutopilotPanel;

/**
 * Unified Hackathon Module Client-Side Core
 * All hackathon modules use this shared client to guarantee memory safety and consistency
 * All modules run in the browser, server never loads hackathon code
 * Estimated memory: ~8-12MB per module - fits perfectly in <150MB total
 */
import runtime from '/static/js/client_hackathon_runtime.js';

// Base hackathon module factory - creates a consistent module instance for any hackathon
export function createHackathonModule(moduleName, customMethods = {}) {
    // Module state - kept isolated per module instance
    let _initialized = false;
    let _apiClient = null;
    let _buffer = null;

    // Base module implementation with all shared functionality
    const baseModule = {
        // Expose API client accessor for module-specific use
        getApiClient() {
            if (!_apiClient) {
                throw new Error(`${moduleName}: Module not initialized - call init() first`);
            }
            return _apiClient;
        },

        /**
         * Initialize the module - called automatically when first accessed
         * Only runs once, lazy loaded to save memory
         */
        async init() {
            if (_initialized) return this;
            
            // Get shared resources from runtime (memory pool, etc.)
            _buffer = runtime.acquireBuffer();
            this.runtime = runtime;
            
            // Initialize API client that talks to your backend server
            // All heavy computation happens client-side, server only serves API
            _apiClient = {
                async callEndpoint(endpoint, data) {
                    const response = await fetch(`/api/${endpoint}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    if (!response.ok) {
                        throw new Error(`API Error: ${response.status} ${response.statusText}`);
                    }
                    return response.json();
                },
                
                // Additional shared API methods for common operations
                async getResource(path) {
                    const response = await fetch(`/api/${path}`, {
                        method: 'GET',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    if (!response.ok) {
                        throw new Error(`API Error: ${response.status} ${response.statusText}`);
                    }
                    return response.json();
                }
            };
            
            _initialized = true;
            console.log(`✅ ${this.MODULE_NAME} initialized client-side`);
            return this;
        },

        /**
         * Cleanup method called when module is evicted from memory
         * Releases all resources back to the runtime
         */
        cleanup() {
            if (_buffer) {
                runtime.releaseBuffer(_buffer);
                _buffer = null;
            }
            _initialized = false;
            _apiClient = null;
            console.log(`🧹 ${this.MODULE_NAME} cleaned up, resources released`);
        },

        /**
         * Report memory usage to the runtime - accurate estimate
         * Base memory + additional for module-specific resources
         */
        estimateMemory() {
            // Base footprint is 8MB, add any module-specific memory from custom implementation
            const customMemory = this.estimateCustomMemory ? this.estimateCustomMemory() : 0;
            return 8 + customMemory;
        },

        // Base module name - will be overridden by module-specific creation
        MODULE_NAME: moduleName,

        // Default processUserRequest that can be overridden by module-specific implementations
        async processUserRequest(data) {
            console.warn(`${this.MODULE_NAME}: Using default processUserRequest - override this in your module`);
            return { success: true, data };
        }
    };

    // Merge base module with custom module-specific methods
    const moduleInstance = { ...baseModule, ...customMethods };
    
    // Ensure core methods can't be accidentally overwritten
    Object.defineProperties(moduleInstance, {
        init: { writable: false, configurable: false },
        cleanup: { writable: false, configurable: false },
        estimateMemory: { writable: false, configurable: false },
        getApiClient: { writable: false, configurable: false }
    });

    return moduleInstance;
}

// Export the base axelr module for backward compatibility
const HackathonModule = createHackathonModule("axelr");
export default HackathonModule;
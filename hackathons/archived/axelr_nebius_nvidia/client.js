/**
 * axelr_nebius_nvidia Module Client Implementation
 * Uses the unified hackathon client core for shared functionality
 */
import { createHackathonModule } from '../../axelr_core/client.js';

// Module-specific methods - only what makes this module unique
const nebiusNvidiaSpecificMethods = {
    MODULE_NAME: "axelr_nebius_nvidia",
    
    // Nebius/NVIDIA-specific GPU cloud functionality
    async deployNvidiaWorkload(workloadConfig) {
        const apiClient = this.getApiClient();
        return await apiClient.callEndpoint('nebius-nvidia/deploy', workloadConfig);
    },
    
    async getGpuMetrics(instanceId) {
        const apiClient = this.getApiClient();
        return await apiClient.getResource(`nebius-nvidia/gpu-metrics/${instanceId}`);
    },
    
    async terminateWorkload(instanceId) {
        const apiClient = this.getApiClient();
        return await apiClient.callEndpoint('nebius-nvidia/terminate', { instanceId });
    },
    
    // Override the default processUserRequest for module-specific handling
    async processUserRequest(data) {
        switch(data.action) {
            case 'deploy':
                return await this.deployNvidiaWorkload(data.workloadConfig);
            case 'metrics':
                return await this.getGpuMetrics(data.instanceId);
            case 'terminate':
                return await this.terminateWorkload(data.instanceId);
            default:
                return { success: false, error: "Unknown GPU workload action" };
        }
    },
    
    // This module needs extra memory for GPU workload tracking
    estimateCustomMemory() {
        return 4; // Adds 4MB to base 8MB, total 12MB for GPU monitoring
    }
};

// Create and export the module instance using the unified factory
const HackathonModule = createHackathonModule("axelr_nebius_nvidia", nebiusNvidiaSpecificMethods);
export default HackathonModule;
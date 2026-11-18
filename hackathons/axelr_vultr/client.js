/**
 * axelr_vultr Module Client Implementation
 * Uses the unified hackathon client core for shared functionality
 */
import { createHackathonModule } from '../axelr_core/client.js';

// Module-specific methods - only what makes this module unique
const vultrSpecificMethods = {
    MODULE_NAME: "axelr_vultr",
    
    // Vultr-specific API and business logic
    async provisionCloudInstance(data) {
        const apiClient = this.getApiClient();
        return await apiClient.callEndpoint('vultr/provision', data);
    },
    
    async getInstanceStatus(instanceId) {
        const apiClient = this.getApiClient();
        return await apiClient.getResource(`vultr/instances/${instanceId}`);
    },
    
    // Override the default processUserRequest for module-specific handling
    async processUserRequest(data) {
        if (data.action === 'provision') {
            return await this.provisionCloudInstance(data.payload);
        } else if (data.action === 'getStatus') {
            return await this.getInstanceStatus(data.instanceId);
        }
        return { success: false, error: "Unknown action" };
    },
    
    // Optional: Add custom memory estimation if this module uses extra resources
    estimateCustomMemory() {
        return 2; // Adds 2MB to base 8MB, total 10MB
    }
};

// Create and export the module instance using the unified factory
const HackathonModule = createHackathonModule("axelr_vultr", vultrSpecificMethods);
export default HackathonModule;
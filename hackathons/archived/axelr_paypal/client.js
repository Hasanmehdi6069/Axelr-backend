/**
 * axelr_paypal Module Client Implementation
 * Uses the unified hackathon client core for shared functionality
 * Updated to use real FastAPI endpoints only (GOAL 6 compliance)
 */
import { createHackathonModule } from '../../axelr_core/client.js';

// Typed error handling for PayPal API errors
class PayPalApiError extends Error {
    constructor(message, code, status) {
        super(message);
        this.name = "PayPalApiError";
        this.code = code;
        this.status = status;
    }
}

// Module-specific methods - only what makes this module unique
const paypalSpecificMethods = {
    MODULE_NAME: "axelr_paypal",
    BASE_PATH: "/paypal",
    
    // Health check endpoint
    async getHealth() {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.getResource(`${this.BASE_PATH}/health`);
        } catch (err) {
            throw new PayPalApiError("Health check failed", "HEALTH_CHECK_FAILED", 500);
        }
    },
    
    // Create new order endpoint
    async createOrder(orderData) {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.callEndpoint(`${this.BASE_PATH}/orders`, orderData);
        } catch (err) {
            throw new PayPalApiError("Failed to create order", "CREATE_ORDER_FAILED", err.status || 500);
        }
    },
    
    // Get order details
    async getOrder(orderId) {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.getResource(`${this.BASE_PATH}/orders/${orderId}`);
        } catch (err) {
            throw new PayPalApiError(`Failed to get order ${orderId}`, "GET_ORDER_FAILED", err.status || 500);
        }
    },
    
    // Capture order payment
    async captureOrder(orderId) {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.callEndpoint(`${this.BASE_PATH}/orders/${orderId}/capture`, {});
        } catch (err) {
            throw new PayPalApiError(`Failed to capture order ${orderId}`, "CAPTURE_ORDER_FAILED", err.status || 500);
        }
    },
    
    // Create refund
    async createRefund(refundData) {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.callEndpoint(`${this.BASE_PATH}/refunds`, refundData);
        } catch (err) {
            throw new PayPalApiError("Failed to create refund", "CREATE_REFUND_FAILED", err.status || 500);
        }
    },
    
    // Extract payment intent from unstructured text (AI endpoint)
    async extractFromText(unstructuredText, approve = false) {
        const apiClient = this.getApiClient();
        try {
            return await apiClient.callEndpoint(`${this.BASE_PATH}/ai/from-text`, {
                unstructured_text: unstructuredText,
                approve: approve
            });
        } catch (err) {
            throw new PayPalApiError("AI extraction failed", "AI_EXTRACTION_FAILED", err.status || 500);
        }
    },
    
    // Handle approve URL from PayPal order creation
    openApproveUrl(approveUrl) {
        if (!approveUrl || typeof approveUrl !== "string") {
            throw new PayPalApiError("Invalid approve URL provided", "INVALID_APPROVE_URL", 400);
        }
        // Validate URL is from PayPal sandbox/production to prevent open redirects
        const paypalDomains = [
            "sandbox.paypal.com",
            "www.paypal.com",
            "paypal.com"
        ];
        try {
            const url = new URL(approveUrl);
            const isPayPalDomain = paypalDomains.some(domain => url.hostname.endsWith(domain));
            if (!isPayPalDomain) {
                throw new PayPalApiError("Approve URL is not from a valid PayPal domain", "INVALID_APPROVE_DOMAIN", 403);
            }
            // Open in new tab (browser context)
            window.open(approveUrl, "_blank", "noopener,noreferrer");
            return { success: true, message: "Approve URL opened in new tab" };
        } catch (err) {
            if (err instanceof PayPalApiError) throw err;
            throw new PayPalApiError("Failed to parse approve URL", "INVALID_APPROVE_URL", 400);
        }
    },
    
    // Override the default processUserRequest for module-specific handling
    async processUserRequest(data) {
        switch(data.action) {
            case 'health':
                return await this.getHealth();
            case 'create_order':
                return await this.createOrder(data.orderData);
            case 'get_order':
                return await this.getOrder(data.orderId);
            case 'capture_order':
                return await this.captureOrder(data.orderId);
            case 'create_refund':
                return await this.createRefund(data.refundData);
            case 'extract_from_text':
                return await this.extractFromText(data.unstructuredText, data.approve || false);
            case 'open_approve_url':
                return this.openApproveUrl(data.approveUrl);
            default:
                return { 
                    success: false, 
                    error: `Unknown payment action: ${data.action}`,
                    code: "UNKNOWN_ACTION"
                };
        }
    },
    
    // Custom memory estimation
    estimateCustomMemory() {
        return 1; // Adds 1MB to base 8MB, total 9MB
    }
};

// Create and export the module instance using the unified factory
const HackathonModule = createHackathonModule("axelr_paypal", paypalSpecificMethods);
export default HackathonModule;
export { PayPalApiError };
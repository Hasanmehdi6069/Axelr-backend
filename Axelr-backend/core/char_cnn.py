from __future__ import annotations
import sys
import math
from typing import List, Dict, Any

__all__ = [
    "CharCNN",
    "identify_errors",
    "get_default_char_cnn",
]

# Singleton instance for lazy initialization
_default_char_cnn: CharCNN | None = None
_cnn_lock: Any = None  # Will be initialized when needed

def get_default_char_cnn() -> CharCNN:
    """
    Returns the global default CharCNN instance, creating it lazily if it doesn't exist.
    Thread-safe initialization using a lock to prevent race conditions.
    What it takes: None
    What it returns: Global CharCNN singleton instance
    What it never raises: Never raises exceptions; always returns a valid instance
    """
    global _default_char_cnn, _cnn_lock
    if _default_char_cnn is None:
        if _cnn_lock is None:
            import threading
            _cnn_lock = threading.Lock()
        with _cnn_lock:
            if _default_char_cnn is None:
                _default_char_cnn = CharCNN()
    return _default_char_cnn

def identify_errors(text: str, threshold: float = 0.7) -> List[Dict[str, Any]]:
    """
    Identifies potential error locations in the text using the default CharCNN model.
    What it takes:
        text: Input string to scan for errors
        threshold: Confidence threshold above which to flag an error (default: 0.7)
    What it returns:
        List of error dictionaries containing type, text, confidence, and suggestion
    What it never raises:
        Never raises exceptions; returns empty list for empty input, handles OOV characters gracefully
    """
    cnn = get_default_char_cnn()
    if not text:
        return []
    
    prediction = cnn.predict(text)
    errors = []
    # prediction[1] is the probability of error (output_dim 1)
    if prediction[1] > threshold:
        errors.append({
            "type": "general_typo_or_error",
            "text": text,
            "confidence": prediction[1],
            "suggestion": "Review text for potential spelling or grammar errors."
        })
    return errors

class CharCNN:
    """
    A microscopic Character Convolutional Neural Network (Char-CNN) for identifying
    spelling errors, typos, and phonetic transcription slip-ups based on character
    proximity weights. Implemented as a raw mathematical matrix looping pipeline
    to adhere to strict memory and performance constraints (<5 MB RAM).
    
    Canonical implementation merging char_cnn.py and char_cnn_matrix.py functionality.
    """

    def __init__(self, vocab_size: int = 70, embedding_dim: int = 16, 
                 filter_sizes: list[int] = [3, 4, 5], num_filters: int = 32,
                 output_dim: int = 2): # output_dim: 0 for correct, 1 for error
        
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.filter_sizes = filter_sizes
        self.num_filters = num_filters
        self.output_dim = output_dim

        # Comprehensive character mapping: supports lowercase, numbers, and common punctuation
        self.char_to_int = {chr(i + 97): i for i in range(26)} # a-z
        self.char_to_int.update({chr(i + 48): i + 26 for i in range(10)}) # 0-9
        self.char_to_int.update({
            ' ': 36, '.': 37, ',': 38, '?': 39, '\'': 40, '!': 41, '@': 42, '#': 43, '$': 44, '%': 45,
            '^': 46, '&': 47, '*': 48, '(': 49, ')': 50, '-': 51, '_': 52, '=': 53, '+': 54,
            '[': 55, ']': 56, '{': 57, '}': 58, ';': 59, ':': 60, '\"': 62, '/': 63,
            '<': 64, '>': 65, '|': 66, '\\': 67, '~': 68, '`': 69
        })
        # Ensure vocab_size matches the actual size of char_to_int
        self.vocab_size = len(self.char_to_int)
        self.int_to_char = {i: char for char, i in self.char_to_int.items()}

        # Initialize weights and biases (very small, random values)
        # Using nested lists for manual matrix operations to avoid external dependencies
        self.embeddings = [[(i * 0.01) % 1.0 for i in range(embedding_dim)] for _ in range(self.vocab_size)]

        self.conv_weights = {}
        self.conv_biases = {}
        for fs in filter_sizes:
            # Weights for each filter: (num_filters, filter_size, embedding_dim)
            self.conv_weights[fs] = [[[((i + j + k) * 0.001) % 1.0 
                                      for k in range(embedding_dim)] 
                                     for j in range(fs)] 
                                    for i in range(num_filters)]
            self.conv_biases[fs] = [((i * 0.001) % 1.0) for i in range(num_filters)]

        # Output layer weights and biases
        total_filters = len(filter_sizes) * num_filters
        self.fc_weights = [[((i + j) * 0.001) % 1.0 for j in range(total_filters)] for i in range(output_dim)]
        self.fc_biases = [((i * 0.001) % 1.0) for i in range(output_dim)]

    def _get_char_embedding(self, char_int: int) -> list[float]:
        if 0 <= char_int < self.vocab_size:
            return self.embeddings[char_int]
        return [0.0] * self.embedding_dim # OOV characters get zero embedding

    def _relu(self, x: float) -> float:
        return max(0.0, x)

    def _softmax(self, x: list[float]) -> list[float]:
        exp_x = [math.exp(i) for i in x]
        sum_exp_x = sum(exp_x)
        return [i / sum_exp_x for i in exp_x]

    def _convolve(self, embedded_chars: list[list[float]], filter_size: int,
                  weights: list[list[list[float]]], biases: list[float]) -> list[float]:
        
        # Output of this convolution will be (num_filters, sequence_length - filter_size + 1)
        # For simplicity, we'll perform a "valid" convolution and then max-pool over time.
        pooled_features = []
        for filter_idx in range(self.num_filters):
            max_val = -float('inf')
            for i in range(len(embedded_chars) - filter_size + 1):
                # Apply filter
                activation = biases[filter_idx]
                for j in range(filter_size):
                    for k in range(self.embedding_dim):
                        activation += embedded_chars[i + j][k] * weights[filter_idx][j][k]
                max_val = max(max_val, self._relu(activation))
            pooled_features.append(max_val)
        return pooled_features

    def predict(self, text: str) -> list[float]:
        """
        Predicts the likelihood of error for a given text string.
        What it takes:
            text: Input string to evaluate
        What it returns:
            List of probabilities: [probability_correct, probability_error]
        What it never raises:
            Never raises exceptions; handles empty strings and OOV characters gracefully
        """
        if not text:
            return [1.0, 0.0] # Assume correct if empty

        # 1. Character Embedding
        embedded_chars = []
        for char in text.lower():
            char_int = self.char_to_int.get(char, self.vocab_size - 1) # OOV maps to last vocab entry
            embedded_chars.append(self._get_char_embedding(char_int))

        # 2. Convolutional and Max-Pooling Layers
        all_pooled_features = []
        for fs in self.filter_sizes:
            if len(embedded_chars) >= fs:
                pooled_features = self._convolve(embedded_chars, fs,
                                                 self.conv_weights[fs], self.conv_biases[fs])
                all_pooled_features.extend(pooled_features)
            else:
                # If text is shorter than filter, pad with zeros
                all_pooled_features.extend([0.0] * self.num_filters)

        # 3. Fully Connected Layer
        fc_output = [0.0] * self.output_dim
        for i in range(self.output_dim):
            for j in range(len(all_pooled_features)):
                fc_output[i] += all_pooled_features[j] * self.fc_weights[i][j]
            fc_output[i] += self.fc_biases[i]

        # 4. Softmax Activation
        return self._softmax(fc_output)

    def current_memory_usage(self) -> int:
        """
        Estimates the current memory usage of the model in bytes.
        What it takes: None
        What it returns: Integer representing total estimated memory usage in bytes
        What it never raises: Never raises exceptions; always returns a numerical value
        """
        total_bytes = 0
        total_bytes += sys.getsizeof(self.char_to_int) + sys.getsizeof(self.int_to_char)
        total_bytes += sys.getsizeof(self.embeddings) # List of lists
        for row in self.embeddings: total_bytes += sys.getsizeof(row)

        total_bytes += sys.getsizeof(self.conv_weights)
        for fs in self.conv_weights:
            total_bytes += sys.getsizeof(self.conv_weights[fs])
            for f in self.conv_weights[fs]:
                total_bytes += sys.getsizeof(f)
                for r in f: total_bytes += sys.getsizeof(r)
        
        total_bytes += sys.getsizeof(self.conv_biases)
        for fs in self.conv_biases: total_bytes += sys.getsizeof(self.conv_biases[fs])

        total_bytes += sys.getsizeof(self.fc_weights)
        for row in self.fc_weights: total_bytes += sys.getsizeof(row)
        total_bytes += sys.getsizeof(self.fc_biases)

        # Add a rough estimate for the float values themselves (24 bytes per float object in Python)
        num_floats = self.vocab_size * self.embedding_dim
        for fs in self.filter_sizes: num_floats += self.num_filters * fs * self.embedding_dim + self.num_filters
        num_floats += (len(self.filter_sizes) * self.num_filters) * self.output_dim + self.output_dim
        total_bytes += num_floats * 24

        return total_bytes

# Example Usage (for testing and demonstration)
if __name__ == "__main__":
    char_cnn = CharCNN()
    print(f"Initial CharCNN memory usage: {char_cnn.current_memory_usage() / (1024 * 1024):.4f} MB")

    text1 = "hello world"
    prediction1 = char_cnn.predict(text1)
    print(f"Text: \"{text1}\" -> Prediction: {prediction1}")
    errors1 = identify_errors(text1)
    print(f"Errors detected: {errors1}")

    text2 = "helo wordl" # Typo
    prediction2 = char_cnn.predict(text2)
    print(f"Text: \"{text2}\" -> Prediction: {prediction2}")
    errors2 = identify_errors(text2)
    print(f"Errors detected: {errors2}")

    text3 = "thiss is a testt"
    prediction3 = char_cnn.predict(text3)
    print(f"Text: \"{text3}\" -> Prediction: {prediction3}")
    errors3 = identify_errors(text3)
    print(f"Errors detected: {errors3}")

    text4 = "a"
    prediction4 = char_cnn.predict(text4)
    print(f"Text: \"{text4}\" -> Prediction: {prediction4}")

    text5 = ""
    prediction5 = char_cnn.predict(text5)
    print(f"Text: \"{text5}\" -> Prediction: {prediction5}")
    errors5 = identify_errors(text5)
    print(f"Errors detected: {errors5}")

    # Test with a longer text to see memory impact (though not truly dynamic)
    long_text = "the quick brown fox jumps over the lazy dog " * 10
    prediction_long = char_cnn.predict(long_text)
    print(f"Long text prediction (memory usage doesn't change dynamically for model weights): {prediction_long}")
    print(f"Final CharCNN memory usage: {char_cnn.current_memory_usage() / (1024 * 1024):.4f} MB")
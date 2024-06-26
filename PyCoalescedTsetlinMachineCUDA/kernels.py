# Copyright (c) 2024 Ole-Christoffer Granmo

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# This code implements the Convolutional Tsetlin Machine from paper arXiv:1905.09688
# https://arxiv.org/abs/1905.09688

code_header = """
	#include <curand_kernel.h>
	
	#define INT_SIZE 32ULL

	#define LA_CHUNKS (((FEATURES-1)/INT_SIZE + 1))
	#define CLAUSE_CHUNKS ((CLAUSES-1)/INT_SIZE + 1)

	#if (FEATURES % 32 != 0)
	#define FILTER (~(0xffffffff << (FEATURES % INT_SIZE)))
	#else
	#define FILTER 0xffffffff
	#endif
"""

code_update = """
	extern "C"
    {
    	// Increment the states of each of those 32 Tsetlin Automata flagged in the active bit vector.
		__device__ inline void inc(unsigned int *ta_state, int clause, int chunk, unsigned int active)
		{
			unsigned int carry, carry_next;
			int id = clause*LA_CHUNKS*STATE_BITS + chunk*STATE_BITS;
			carry = active;
			for (int b = 0; b < STATE_BITS; ++b) {
				if (carry == 0)
					break;

				carry_next = ta_state[id + b] & carry; // Sets carry bits (overflow) passing on to next bit
				ta_state[id + b] = ta_state[id + b] ^ carry; // Performs increments with XOR
				carry = carry_next;
			}

			if (carry > 0) {
				for (int b = 0; b < STATE_BITS; ++b) {
					ta_state[id + b] |= carry;
				}
			}   
		}

		// Decrement the states of each of those 32 Tsetlin Automata flagged in the active bit vector.
		__device__ inline void dec(unsigned int *ta_state, int clause, int chunk, unsigned int active)
		{
			unsigned int carry, carry_next;
			int id = clause*LA_CHUNKS*STATE_BITS + chunk*STATE_BITS;
			carry = active;
			for (int b = 0; b < STATE_BITS; ++b) {
				if (carry == 0)
					break;
				carry_next = (~ta_state[id + b]) & carry; // Sets carry bits (overflow) passing on to next bit
				ta_state[id + b] = ta_state[id + b] ^ carry; // Performs increments with XOR
				carry = carry_next;
			}

			if (carry > 0) {
				for (int b = 0; b < STATE_BITS; ++b) {
					ta_state[id + b] &= ~carry;
				}
			} 
		}

		__device__ inline void calculate_clause_output(curandState *localState, unsigned int *ta_state, unsigned int *clause_output, int *clause_patch, int *X)
		{
			int output_one_patches[PATCHES];
			int output_one_patches_count;

			// Evaluate each patch (convolution)
			output_one_patches_count = 0;
			for (int patch = 0; patch < PATCHES; ++patch) {
				int patch_clause_output = 1;
				for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
					if ((ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] & X[patch*LA_CHUNKS + la_chunk]) != ta_state[la_chunk*STATE_BITS + STATE_BITS - 1]) {
						patch_clause_output = 0;
						break;
					}
				}

				if (((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & X[patch*LA_CHUNKS + LA_CHUNKS - 1] & FILTER) != (ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER))) {
					patch_clause_output = 0;
				}

				if (patch_clause_output) {
					output_one_patches[output_one_patches_count] = patch;
					output_one_patches_count++;
				}
			}
		
			if (output_one_patches_count > 0) {
				*clause_output = 1;
				int patch_id = curand(localState) % output_one_patches_count;
				*clause_patch = output_one_patches[patch_id];
			} else {
				*clause_output = 0;
				*clause_patch = -1;
			}
		}

		__device__ inline void update_clause(curandState *localState, int *clause_weight, unsigned int *ta_state, int clause_output, int clause_patch, int *X, int y, int class_sum)
		{
            printf("y = %d, class_sum = %d, clause_output = %d, clause_patch = %d, *clause_weight = %d\\n", y, class_sum, clause_output, clause_patch, *clause_weight);
			int target = 1 - 2*(class_sum > y);
            printf("target = %d\\n", target);
			
			if (target == -1 && curand_uniform(localState) > 1.0*Q/max(1, CLASSES-1)) {
				return;
			}

			int sign = (*clause_weight >= 0) - (*clause_weight < 0);
            printf("sign = %d\\n", sign);
		
			int absolute_prediction_error = abs(y - class_sum);
			if (curand_uniform(localState) <= 1.0*absolute_prediction_error/(2*THRESHOLD)) {
				if (target*sign > 0) {
                    printf("TYPE I\\n");
					if (clause_output && abs(*clause_weight) < INT_MAX) {
						(*clause_weight) += sign;
					}


					// Type I Feedback
					for (int la_chunk = 0; la_chunk < LA_CHUNKS; ++la_chunk) {
						// Generate random bit values
						unsigned int la_feedback = 0;
						for (int b = 0; b < INT_SIZE; ++b) {
							if (curand_uniform(localState) <= 1.0/S) {
								la_feedback |= (1 << b);
							}
						}

                        printf("la_chunk = %d, la_feedback = %d, X[...] = %d\\n", la_chunk, la_feedback, X[clause_patch*LA_CHUNKS + la_chunk]);


						if (clause_output) {
							#if BOOST_TRUE_POSITIVE_FEEDBACK == 1
								inc(ta_state, 0, la_chunk, X[clause_patch*LA_CHUNKS + la_chunk]);
							#else
								inc(ta_state, 0, la_chunk, X[clause_patch*LA_CHUNKS + la_chunk] & (~la_feedback));
							#endif

							dec(ta_state, 0, la_chunk, (~X[clause_patch*LA_CHUNKS + la_chunk]) & la_feedback);
						} else {
							dec(ta_state, 0, la_chunk, la_feedback);
						}
					}
				} else if (target*sign < 0 && clause_output) {
					// Type II Feedback

                    printf("TYPE II\\n");
					(*clause_weight) -= sign;
					#if NEGATIVE_CLAUSES == 0
						if (*clause_weight < 1) {
							*clause_weight = 1;
						}
					#endif

					for (int la_chunk = 0; la_chunk < LA_CHUNKS; ++la_chunk) {
						inc(ta_state, 0, la_chunk, (~X[clause_patch*LA_CHUNKS + la_chunk]) & (~ta_state[la_chunk*STATE_BITS + STATE_BITS - 1]));
					}
				}
			}
		}

		// Evaluate example
		__global__ void evaluate(unsigned int *global_ta_state, int *clause_weights, int *class_sum, int *X, int example)
		{
            printf("Inside evaluate \\n");
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

            printf("index = %d, stride = %d\\n", index, stride);

			for (int clause = index; clause < CLAUSES; clause += stride) {
				unsigned int *ta_state = &global_ta_state[clause*LA_CHUNKS*STATE_BITS];

				int clause_output;
				for (int patch = 0; patch < PATCHES; ++patch) {
					clause_output = 1;
					for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
						if ((ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] & X[(unsigned long long)example*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + la_chunk]) != ta_state[la_chunk*STATE_BITS + STATE_BITS - 1]) {
							clause_output = 0;
							break;
						}
					}

					if ((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & X[(unsigned long long)example*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + LA_CHUNKS-1] & FILTER) != (ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER)) {
						clause_output = 0;
					}

					if (clause_output) {
						break;
					}
				}

				if (clause_output) {
					for (int class_id = 0; class_id < CLASSES; ++class_id) {
						int clause_weight = clause_weights[class_id*CLAUSES + clause];
                        printf("class_id = %d, clause_weight = %d\\n", class_id, clause_weight);
						atomicAdd(&class_sum[class_id], clause_weight);
                        printf("\tclass_id = %d, clause_weight = %d\\n", class_id, clause_weight);
					}
				}
			}
		}

		// Update state of Tsetlin Automata team
		__global__ void update(curandState *state, unsigned int *global_ta_state, int *clause_weights, int *class_sum, int *X, int *y, int example)
		{
            printf("Inside update \\n");
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;
            printf("index = %d, stride = %d\\n", index, stride);

			/* Copy state to local memory for efficiency */
			curandState localState = state[index];

			// Calculate clause output first
			for (unsigned long long clause = index; clause < CLAUSES; clause += stride) {
				unsigned int *ta_state = &global_ta_state[clause*LA_CHUNKS*STATE_BITS];

				unsigned int clause_output;
				int clause_patch;
				calculate_clause_output(&localState, ta_state, &clause_output, &clause_patch, &X[(unsigned long long)example*(LA_CHUNKS*PATCHES)]);

				for (unsigned long long class_id = 0; class_id < CLASSES; ++class_id) {
					int local_class_sum = class_sum[class_id];
					if (local_class_sum > THRESHOLD) {
						local_class_sum = THRESHOLD;
					} else if (local_class_sum < -THRESHOLD) {
						local_class_sum = -THRESHOLD;
					}
                    printf("class_id = %d, local_class_sum = %d\\n", class_id, local_class_sum);
                    printf("Going to update_clause\\n");
					update_clause(&localState, &clause_weights[class_id*CLAUSES + clause], ta_state, clause_output, clause_patch, &X[(unsigned long long)example*(LA_CHUNKS*PATCHES)], y[example*CLASSES + class_id], local_class_sum);
				}
			}
		
			state[index] = localState;
		}
    }
"""

code_evaluate = """
	extern "C"
    {
		// Evaluate examples
		__global__ void evaluate(unsigned int *global_ta_state, int *clause_weights, int *class_sum, int *X)
		{
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

			for (int clause = index; clause < CLAUSES; clause += stride) {
				unsigned int *ta_state = &global_ta_state[clause*LA_CHUNKS*STATE_BITS];

				int all_exclude = 1;
				for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
					if (ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] > 0) {
						all_exclude = 0;
						break;
					}
				}

				if ((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER) > 0) {
					all_exclude = 0;
				}

				if (all_exclude) {
					continue;
				}

				for (unsigned long long e = 0; e < NUMBER_OF_EXAMPLES; ++e) {
					int clause_output;
					for (int patch = 0; patch < PATCHES; ++patch) {
						clause_output = 1;
						for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
							if ((ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] & X[e*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + la_chunk]) != ta_state[la_chunk*STATE_BITS + STATE_BITS - 1]) {
								clause_output = 0;
								break;
							}
						}

						if ((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & X[e*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + LA_CHUNKS-1] & FILTER) != (ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER)) {
							clause_output = 0;
						}

						if (clause_output) {
							break;
						}
					}

					if (clause_output) {
						for (int class_id = 0; class_id < CLASSES; ++class_id) {
							int clause_weight = clause_weights[class_id*CLAUSES + clause];
							atomicAdd(&class_sum[class_id*NUMBER_OF_EXAMPLES + e], clause_weight);					
						}
					}
				}
			}
		}
	}
"""

code_prepare = """
	extern "C"
    {
		__global__ void prepare(curandState *state, unsigned int *global_ta_state, int *clause_weights, int *class_sum)
		{
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

			curandState localState = state[index];

			for (unsigned long long clause = index; clause < CLAUSES; clause += stride) {
				for (unsigned long long class_id = 0; class_id < CLASSES; ++class_id) {
					#if NEGATIVE_CLAUSES == 1
						clause_weights[class_id*CLAUSES + clause] = 1 - 2 * (curand(&localState) % 2);
					#else
						clause_weights[class_id*CLAUSES + clause] = 1;
					#endif
				}

				unsigned int *ta_state = &global_ta_state[clause*LA_CHUNKS*STATE_BITS];
				for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
					for (int b = 0; b < STATE_BITS-1; ++b) {
						ta_state[la_chunk*STATE_BITS + b] = ~0;
					}
					ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] = 0;
				}
			}

			state[index] = localState;
		}
	}
"""

code_encode = """
	#include <curand_kernel.h>

	extern "C"
    {
		__global__ void prepare_encode(unsigned int *X, unsigned int *encoded_X, int number_of_examples, int dim_x, int dim_y, int dim_z, int patch_dim_x, int patch_dim_y, int append_negated, int class_features)
		{
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

			int number_of_features = class_features + patch_dim_x * patch_dim_y * dim_z + (dim_x - patch_dim_x) + (dim_y - patch_dim_y);
			int number_of_patches = (dim_x - patch_dim_x + 1) * (dim_y - patch_dim_y + 1);

			int number_of_ta_chunks;
			if (append_negated) {
				number_of_ta_chunks= (((2*number_of_features-1)/32 + 1));
			} else {
				number_of_ta_chunks= (((number_of_features-1)/32 + 1));
			}

			for (unsigned long long i = index; i < number_of_examples * number_of_patches * number_of_ta_chunks; i += stride) {
				encoded_X[i] = 0;
			}
		}
	
		__global__ void encode(unsigned int *X, unsigned int *encoded_X, int number_of_examples, int dim_x, int dim_y, int dim_z, int patch_dim_x, int patch_dim_y, int append_negated, int class_features)
		{
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

			int global_number_of_features = dim_x * dim_y * dim_z;
			int number_of_features = class_features + patch_dim_x * patch_dim_y * dim_z + (dim_x - patch_dim_x) + (dim_y - patch_dim_y);
			int number_of_patches = (dim_x - patch_dim_x + 1) * (dim_y - patch_dim_y + 1);

			int number_of_ta_chunks;
			if (append_negated) {
				number_of_ta_chunks= (((2*number_of_features-1)/32 + 1));
			} else {
				number_of_ta_chunks= (((number_of_features-1)/32 + 1));
			}

			unsigned int *Xi;
			unsigned int *encoded_Xi;

			unsigned int input_step_size = global_number_of_features;

			for (unsigned long long i = index; i < number_of_examples; i += stride) {
				unsigned long long encoded_pos = i * number_of_patches * number_of_ta_chunks;
				unsigned long long input_pos = i * input_step_size;

				int patch_nr = 0;
				// Produce the patches of the current image
				for (int y = 0; y < dim_y - patch_dim_y + 1; ++y) {
					for (int x = 0; x < dim_x - patch_dim_x + 1; ++x) {
						Xi = &X[input_pos];
						encoded_Xi = &encoded_X[encoded_pos];

						// Encode class into feature vector 
						for (int class_feature = 0; class_feature < class_features; ++class_feature) {

							int chunk_nr = (class_feature + number_of_features) / 32;
							int chunk_pos = (class_feature + number_of_features) % 32;
							encoded_Xi[chunk_nr] |= (1 << chunk_pos);
						}

						// Encode y coordinate of patch into feature vector 
						for (int y_threshold = 0; y_threshold < dim_y - patch_dim_y; ++y_threshold) {
							int patch_pos = class_features + y_threshold;

							if (y > y_threshold) {
								int chunk_nr = patch_pos / 32;
								int chunk_pos = patch_pos % 32;
								encoded_Xi[chunk_nr] |= (1 << chunk_pos);
							} else if (append_negated) {
								int chunk_nr = (patch_pos + number_of_features) / 32;
								int chunk_pos = (patch_pos + number_of_features) % 32;
								encoded_Xi[chunk_nr] |= (1 << chunk_pos);
							}
						}

						// Encode x coordinate of patch into feature vector
						for (int x_threshold = 0; x_threshold < dim_x - patch_dim_x; ++x_threshold) {
							int patch_pos = class_features + (dim_y - patch_dim_y) + x_threshold;

							if (x > x_threshold) {
								int chunk_nr = patch_pos / 32;
								int chunk_pos = patch_pos % 32;

								encoded_Xi[chunk_nr] |= (1 << chunk_pos);
							} else if (append_negated) {
								int chunk_nr = (patch_pos + number_of_features) / 32;
								int chunk_pos = (patch_pos + number_of_features) % 32;
								encoded_Xi[chunk_nr] |= (1 << chunk_pos);
							}
						} 

						// Encode patch content into feature vector
						for (int p_y = 0; p_y < patch_dim_y; ++p_y) {
							for (int p_x = 0; p_x < patch_dim_x; ++p_x) {
								for (int z = 0; z < dim_z; ++z) {
									int image_pos = (y + p_y)*dim_x*dim_z + (x + p_x)*dim_z + z;
									int patch_pos = class_features + (dim_y - patch_dim_y) + (dim_x - patch_dim_x) + p_y * patch_dim_x * dim_z + p_x * dim_z + z;

									if (Xi[image_pos] == 1) {
										int chunk_nr = patch_pos / 32;
										int chunk_pos = patch_pos % 32;
										encoded_Xi[chunk_nr] |= (1 << chunk_pos);
									} else if (append_negated) {
										int chunk_nr = (patch_pos + number_of_features) / 32;
										int chunk_pos = (patch_pos + number_of_features) % 32;
										encoded_Xi[chunk_nr] |= (1 << chunk_pos);
									}
								}
							}
						}
						encoded_pos += number_of_ta_chunks;
						patch_nr++;
					}
				}
			}
		}
	}
"""

code_transform = """
	extern "C"
    {
		// Transform examples
		__global__ void transform(unsigned int *global_ta_state, int *X, int *transformed_X)
		{
			int index = blockIdx.x * blockDim.x + threadIdx.x;
			int stride = blockDim.x * gridDim.x;

			for (int j = index; j < CLAUSES; j += stride) {
				unsigned int *ta_state = &global_ta_state[j*LA_CHUNKS*STATE_BITS];

				int all_exclude = 1;
				for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
					if (ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] > 0) {
						all_exclude = 0;
						break;
					}
				}

				if ((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER) > 0) {
					all_exclude = 0;
				}

				if (all_exclude) {
					for (unsigned long long e = 0; e < NUMBER_OF_EXAMPLES; ++e) {
						transformed_X[e*CLAUSES + j] = 0;
					}
					
					continue;
				}

				for (int e = 0; e < NUMBER_OF_EXAMPLES; ++e) {
					int clause_output;
					for (int patch = 0; patch < PATCHES; ++patch) {
						clause_output = 1;
						for (int la_chunk = 0; la_chunk < LA_CHUNKS-1; ++la_chunk) {
							if ((ta_state[la_chunk*STATE_BITS + STATE_BITS - 1] & X[e*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + la_chunk]) != ta_state[la_chunk*STATE_BITS + STATE_BITS - 1]) {
								clause_output = 0;
								break;
							}
						}

						if ((ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & X[e*(LA_CHUNKS*PATCHES) + patch*LA_CHUNKS + LA_CHUNKS-1] & FILTER) != (ta_state[(LA_CHUNKS-1)*STATE_BITS + STATE_BITS - 1] & FILTER)) {
							clause_output = 0;
						}

						if (clause_output) {
							break;
						}
					}

					transformed_X[e*CLAUSES + j] = clause_output;
				}
			}
		}
	}
"""

const INITIAL_STATE: [u32; 8] = [
    0x6a09_e667,
    0xbb67_ae85,
    0x3c6e_f372,
    0xa54f_f53a,
    0x510e_527f,
    0x9b05_688c,
    0x1f83_d9ab,
    0x5be0_cd19,
];

const ROUND_CONSTANTS: [u32; 64] = [
    0x428a_2f98,
    0x7137_4491,
    0xb5c0_fbcf,
    0xe9b5_dba5,
    0x3956_c25b,
    0x59f1_11f1,
    0x923f_82a4,
    0xab1c_5ed5,
    0xd807_aa98,
    0x1283_5b01,
    0x2431_85be,
    0x550c_7dc3,
    0x72be_5d74,
    0x80de_b1fe,
    0x9bdc_06a7,
    0xc19b_f174,
    0xe49b_69c1,
    0xefbe_4786,
    0x0fc1_9dc6,
    0x240c_a1cc,
    0x2de9_2c6f,
    0x4a74_84aa,
    0x5cb0_a9dc,
    0x76f9_88da,
    0x983e_5152,
    0xa831_c66d,
    0xb003_27c8,
    0xbf59_7fc7,
    0xc6e0_0bf3,
    0xd5a7_9147,
    0x06ca_6351,
    0x1429_2967,
    0x27b7_0a85,
    0x2e1b_2138,
    0x4d2c_6dfc,
    0x5338_0d13,
    0x650a_7354,
    0x766a_0abb,
    0x81c2_c92e,
    0x9272_2c85,
    0xa2bf_e8a1,
    0xa81a_664b,
    0xc24b_8b70,
    0xc76c_51a3,
    0xd192_e819,
    0xd699_0624,
    0xf40e_3585,
    0x106a_a070,
    0x19a4_c116,
    0x1e37_6c08,
    0x2748_774c,
    0x34b0_bcb5,
    0x391c_0cb3,
    0x4ed8_aa4a,
    0x5b9c_ca4f,
    0x682e_6ff3,
    0x748f_82ee,
    0x78a5_636f,
    0x84c8_7814,
    0x8cc7_0208,
    0x90be_fffa,
    0xa450_6ceb,
    0xbef9_a3f7,
    0xc671_78f2,
];

pub(super) fn sha256(input: &[u8]) -> [u8; 32] {
    let padded = padded_message(input);
    let mut state = INITIAL_STATE;
    for block in padded.chunks_exact(64) {
        compress(&mut state, block);
    }
    digest_bytes(state)
}

fn padded_message(input: &[u8]) -> Vec<u8> {
    let bit_length = (input.len() as u64).wrapping_mul(8);
    let block_count = (input.len() + 9).div_ceil(64);
    let mut padded = vec![0_u8; block_count * 64];
    padded[..input.len()].copy_from_slice(input);
    padded[input.len()] = 0x80;
    let length_offset = padded.len() - 8;
    padded[length_offset..].copy_from_slice(&bit_length.to_be_bytes());
    padded
}

fn compress(state: &mut [u32; 8], block: &[u8]) {
    let words = message_schedule(block);
    let [mut first, mut second, mut third, mut fourth, mut fifth, mut sixth, mut seventh, mut eighth] =
        *state;
    for (index, word) in words.into_iter().enumerate() {
        let sigma_one = fifth.rotate_right(6) ^ fifth.rotate_right(11) ^ fifth.rotate_right(25);
        let choice = (fifth & sixth) ^ (!fifth & seventh);
        let temporary_one = eighth
            .wrapping_add(sigma_one)
            .wrapping_add(choice)
            .wrapping_add(ROUND_CONSTANTS[index])
            .wrapping_add(word);
        let sigma_zero = first.rotate_right(2) ^ first.rotate_right(13) ^ first.rotate_right(22);
        let majority = (first & second) ^ (first & third) ^ (second & third);
        let temporary_two = sigma_zero.wrapping_add(majority);
        eighth = seventh;
        seventh = sixth;
        sixth = fifth;
        fifth = fourth.wrapping_add(temporary_one);
        fourth = third;
        third = second;
        second = first;
        first = temporary_one.wrapping_add(temporary_two);
    }
    for (slot, value) in state
        .iter_mut()
        .zip([first, second, third, fourth, fifth, sixth, seventh, eighth])
    {
        *slot = slot.wrapping_add(value);
    }
}

fn message_schedule(block: &[u8]) -> [u32; 64] {
    let mut words = [0_u32; 64];
    for (index, word) in words[..16].iter_mut().enumerate() {
        let offset = index * 4;
        *word = u32::from_be_bytes([
            block[offset],
            block[offset + 1],
            block[offset + 2],
            block[offset + 3],
        ]);
    }
    for index in 16..64 {
        let sigma_zero = words[index - 15].rotate_right(7)
            ^ words[index - 15].rotate_right(18)
            ^ (words[index - 15] >> 3);
        let sigma_one = words[index - 2].rotate_right(17)
            ^ words[index - 2].rotate_right(19)
            ^ (words[index - 2] >> 10);
        words[index] = words[index - 16]
            .wrapping_add(sigma_zero)
            .wrapping_add(words[index - 7])
            .wrapping_add(sigma_one);
    }
    words
}

fn digest_bytes(state: [u32; 8]) -> [u8; 32] {
    let mut output = [0_u8; 32];
    for (chunk, word) in output.chunks_exact_mut(4).zip(state) {
        chunk.copy_from_slice(&word.to_be_bytes());
    }
    output
}

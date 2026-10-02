"""D3DRS blend state -> Vulkan blend factors."""
import pytest

vk = pytest.importorskip("vulkan")

from tew.api.d3d8._pipeline import _vk_blend_factors


def test_standard_alpha_blend():
    assert _vk_blend_factors(5, 6) == (vk.VK_BLEND_FACTOR_SRC_ALPHA, vk.VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA)


def test_additive_and_multiply_modes_seen_in_dealer_screen():
    assert _vk_blend_factors(5, 2) == (vk.VK_BLEND_FACTOR_SRC_ALPHA, vk.VK_BLEND_FACTOR_ONE)
    assert _vk_blend_factors(9, 1) == (vk.VK_BLEND_FACTOR_DST_COLOR, vk.VK_BLEND_FACTOR_ZERO)


def test_both_src_alpha_overrides_dest():
    assert _vk_blend_factors(12, 1) == (vk.VK_BLEND_FACTOR_SRC_ALPHA, vk.VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA)
    assert _vk_blend_factors(13, 1) == (vk.VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA, vk.VK_BLEND_FACTOR_SRC_ALPHA)


def test_unknown_value_raises():
    with pytest.raises(ValueError):
        _vk_blend_factors(99, 1)

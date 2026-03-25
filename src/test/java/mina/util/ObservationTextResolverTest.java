package mina.util;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ObservationTextResolverTest {
    @Test
    void resolverUsesBundledEnglishLabelsForRegistryBackedObjects() {
        ObservationTextResolver resolver = new ObservationTextResolver("en_us");

        assertEquals(
                "Dark Oak Leaves",
                resolver.translationKeyName("block.minecraft.dark_oak_leaves", "minecraft:dark_oak_leaves")
        );
        assertEquals(
                "Diamond Pickaxe",
                resolver.translationKeyName("item.minecraft.diamond_pickaxe", "minecraft:diamond_pickaxe")
        );
        assertEquals(
                "Zombie",
                resolver.translationKeyName("entity.minecraft.zombie", "minecraft:zombie")
        );
    }

    @Test
    void resolverFallsBackToHumanizedIdWhenTranslationKeyIsMissing() {
        ObservationTextResolver resolver = new ObservationTextResolver("en_us");

        assertEquals(
                "Custom Missing Thing",
                resolver.translationKeyName("missing.translation.key", "minecraft:custom_missing_thing")
        );
    }
}
